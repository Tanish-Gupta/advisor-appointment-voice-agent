"""GatedToolRunner (LLD 3.10-3.11): Gemini proposes -> ToolGate checks -> ToolClient executes.

If Gemini is unavailable, disabled, or any proposal is rejected (or a needed call is missing),
the runner falls back to the plan's own calls, checked by the same gate. Either way the real
FastMCP Google tools are what finally runs.
"""

import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from advisor_agent.mcp_client.tool_gate import GateDecision, ToolGate
from advisor_agent.nlu.tool_agent import (
    AgentStep,
    GeminiToolAgent,
    ProposedCall,
    ToolAgentUnavailable,
)
from advisor_agent.orchestrator.states import State

log = logging.getLogger("advisor_agent.audit")


class ToolCaller(Protocol):
    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class ToolPolicyError(RuntimeError):
    """Even the deterministic fallback was rejected by the gate (a bug, never user-visible)."""


class GatedToolRunner:
    def __init__(
        self,
        client: ToolCaller,
        gate: ToolGate,
        agent: GeminiToolAgent | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.client = client
        self.gate = gate
        self.agent = agent
        self._clock = clock
        self.decisions: deque[dict[str, Any]] = deque(maxlen=500)  # audit trail (no PII)
        self.calls: deque[dict[str, Any]] = deque(maxlen=500)  # executed tool calls

    def _audit(self, source: str, call: ProposedCall, decision: GateDecision) -> None:
        entry = {
            "at": self._clock().isoformat(),
            "source": source,
            "tool": call.tool,
            "decision": decision.label,
        }
        self.decisions.append(entry)
        log.info("tool_gate", extra=entry)

    async def plan(
        self, step: AgentStep, state: State, *, use_agent: bool = True
    ) -> dict[str, ProposedCall]:
        """Return approved calls keyed by tool (args are the plan's canonical values)."""
        if use_agent and self.agent is not None:
            try:
                proposals = await self.agent.propose(step)
            except ToolAgentUnavailable:
                proposals = None
            if proposals is not None:
                approved = self._approve(proposals, step, state, "llm")
                if approved is not None:
                    return approved
        approved = self._approve(step.fallback_calls(), step, state, "fallback")
        if approved is None:
            raise ToolPolicyError(f"fallback rejected for {list(step.expected)} in {state}")
        return approved

    def _approve(
        self, calls: list[ProposedCall], step: AgentStep, state: State, source: str
    ) -> dict[str, ProposedCall] | None:
        result: dict[str, ProposedCall] = {}
        ok = True
        for i, call in enumerate(calls):
            if call.tool not in step.allowed_tools:
                decision = GateDecision(False, "not_allowed_in_step")
            elif call.tool in result:
                decision = GateDecision(False, "duplicate")
            else:
                decision = self.gate.check(call, state=state, expected=step.expected, call_index=i)
            self._audit(source, call, decision)
            if not decision.approved:
                ok = False
                continue
            result[call.tool] = ProposedCall(call.tool, dict(step.expected[call.tool]))
        if not ok or set(result) != set(step.expected):
            if ok:
                log.info("tool_gate", extra={"source": source, "decision": "rejected:incomplete"})
            return None
        return {tool: result[tool] for tool in step.expected}  # the plan's execution order

    async def call(self, call: ProposedCall) -> dict[str, Any]:
        """Execute an approved call through the MCP client (raises ToolCallError)."""
        result = await self.client.call(call.tool, dict(call.args))
        self.calls.append({"at": self._clock().isoformat(), "tool": call.tool})
        return result
