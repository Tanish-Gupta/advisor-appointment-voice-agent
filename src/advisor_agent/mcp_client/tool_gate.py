"""ToolGate (LLD 3.11): deterministic policy for every MCP tool call, whether proposed by Gemini
or by the deterministic fallback. It is code, not a prompt."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from advisor_agent.domain.plan import (
    ALL_TOOLS,
    CREATE_HOLD,
    GMAIL_DRAFT,
    LIST_BUSY,
    WRITE_TOOLS,
    BookingPlan,
)
from advisor_agent.domain.timeutil import to_ist
from advisor_agent.guardrails import pii
from advisor_agent.nlu.tool_agent import ProposedCall
from advisor_agent.orchestrator.states import State

__all__ = ["BookingPlan", "GateDecision", "ProposedCall", "ToolGate", "STATE_TOOLS"]

Clock = Callable[[], datetime]

# Read-only lookups are fine once the disclaimer is acknowledged; writes only in EXECUTE,
# i.e. after the user's explicit "yes".
_NO_TOOLS = {State.START, State.DISCLAIMER_ACK, State.CLOSE}
STATE_TOOLS: dict[State, frozenset[str]] = {
    s: (frozenset() if s in _NO_TOOLS else frozenset({LIST_BUSY})) for s in State
}
STATE_TOOLS[State.EXECUTE] = ALL_TOOLS

# Optional tool parameters and their server defaults: a proposal may omit them.
TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    CREATE_HOLD: {"kind": "booking", "version": 1},
    GMAIL_DRAFT: {"slot": None},
}


@dataclass(frozen=True)
class GateDecision:
    approved: bool
    rule: str | None = None

    @property
    def label(self) -> str:
        return "approved" if self.approved else f"rejected:{self.rule}"


APPROVED = GateDecision(True)


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _normalised(tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    merged = {**TOOL_DEFAULTS.get(tool, {}), **args}
    return {k: _norm(v) for k, v in merged.items()}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


class ToolGate:
    def __init__(
        self,
        clock: Clock,
        *,
        horizon_days: int = 14,
        business_hours: tuple[int, int] = (9, 18),
        max_calls_per_step: int = 4,
    ) -> None:
        self._clock = clock
        self._horizon_days = horizon_days
        self._open = time(business_hours[0])
        self._close = time(business_hours[1])
        self.max_calls = max_calls_per_step

    def check(
        self,
        call: ProposedCall,
        *,
        state: State,
        expected: Mapping[str, Mapping[str, Any]],
        call_index: int = 0,
        already_done: bool = False,
    ) -> GateDecision:
        """`call_index` is the 0-based position of the call within this step (cap rule);
        `already_done` is True when the same `{code}:{tool}:{version}` write already ran."""
        tool = call.tool
        if tool not in ALL_TOOLS:
            return GateDecision(False, "unknown_tool")
        if tool in WRITE_TOOLS and state is not State.EXECUTE:
            return GateDecision(False, "write_before_confirm")
        if tool not in STATE_TOOLS.get(state, frozenset()):
            return GateDecision(False, "not_allowed_in_state")
        if call_index >= self.max_calls:
            return GateDecision(False, "cap")
        if any(pii.contains_pii(s) for s in _strings(call.args)):
            return GateDecision(False, "pii")
        if tool == LIST_BUSY and not self._busy_window_ok(call.args):
            return GateDecision(False, "read_bounds")
        want = expected.get(tool)
        if want is None or _normalised(tool, call.args) != _normalised(tool, want):
            return GateDecision(False, "plan_mismatch")
        if already_done and tool in WRITE_TOOLS:
            return GateDecision(False, "once")
        return APPROVED

    def check_job(self, call: ProposedCall) -> GateDecision:
        """Defence in depth for the outbox worker: known write tool and no PII."""
        if call.tool not in WRITE_TOOLS:
            return GateDecision(False, "unknown_tool")
        if any(pii.contains_pii(s) for s in _strings(call.args)):
            return GateDecision(False, "pii")
        return APPROVED

    def _busy_window_ok(self, args: Mapping[str, Any]) -> bool:
        try:
            start = to_ist(datetime.fromisoformat(str(args["start_ist"])))
            end = to_ist(datetime.fromisoformat(str(args["end_ist"])))
        except (KeyError, ValueError):
            return False
        today = to_ist(self._clock()).date()
        last_day = today + timedelta(days=self._horizon_days + 1)
        if not start < end:
            return False
        if start.date() < today or end.date() > last_day:
            return False
        return start.time() >= self._open and end.time() <= self._close
