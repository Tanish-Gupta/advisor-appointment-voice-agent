"""ToolGate rules and the GatedToolRunner (Gemini proposes -> gate -> fallback)."""

from datetime import date, datetime

import pytest

from advisor_agent.domain.models import Topic
from advisor_agent.domain.plan import (
    BOOKING_WRITES,
    CREATE_HOLD,
    DOCS_APPEND,
    GMAIL_DRAFT,
    LIST_BUSY,
    BookingPlan,
)
from advisor_agent.domain.slots import make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.mcp_client.runner import GatedToolRunner, ToolPolicyError
from advisor_agent.mcp_client.tool_gate import ToolGate
from advisor_agent.nlu.tool_agent import AgentStep, GeminiToolAgent, ProposedCall
from advisor_agent.orchestrator.states import State
from tests.conftest import fixed_clock

SLOT = make_slot(datetime(2026, 10, 6, 13, 0, tzinfo=IST))
PLAN = BookingPlan(code="NL-A742", topic=Topic.SIP_MANDATES, slot=SLOT, booked_on=date(2026, 10, 2))
EXPECTED = PLAN.expected_calls()
STEP = AgentStep(allowed_tools=(LIST_BUSY, *BOOKING_WRITES), facts=PLAN.facts(), expected=EXPECTED)


@pytest.fixture
def gate() -> ToolGate:
    return ToolGate(fixed_clock, horizon_days=14, business_hours=(9, 18), max_calls_per_step=4)


def _call(tool: str, **override: object) -> ProposedCall:
    return ProposedCall(tool, {**EXPECTED[tool], **override})


# --- ToolGate ----------------------------------------------------------------------------


def test_exact_plan_calls_are_approved(gate: ToolGate) -> None:
    for i, tool in enumerate(EXPECTED):
        assert gate.check(
            _call(tool), state=State.EXECUTE, expected=EXPECTED, call_index=i
        ).approved


@pytest.mark.parametrize(
    ("call", "state", "rule"),
    [
        (ProposedCall("gmail_send", {}), State.EXECUTE, "unknown_tool"),
        (_call(CREATE_HOLD), State.CONFIRM_SLOT, "write_before_confirm"),
        (_call(GMAIL_DRAFT), State.OFFER_SLOTS, "write_before_confirm"),
        (_call(LIST_BUSY), State.START, "not_allowed_in_state"),
        (_call(LIST_BUSY), State.DISCLAIMER_ACK, "not_allowed_in_state"),
        (_call(CREATE_HOLD, code="NL-B999"), State.EXECUTE, "plan_mismatch"),
        (_call(CREATE_HOLD, start_ist="2026-10-06T14:00:00+05:30"), State.EXECUTE, "plan_mismatch"),
        (_call(DOCS_APPEND, status="confirmed"), State.EXECUTE, "plan_mismatch"),
        (_call(GMAIL_DRAFT, slot="ring me at +91 98765 43210"), State.EXECUTE, "pii"),
        (_call(DOCS_APPEND, slot="mail a.b@example.com"), State.EXECUTE, "pii"),
        (_call(LIST_BUSY, end_ist="2026-12-30T13:30:00+05:30"), State.EXECUTE, "read_bounds"),
        (_call(LIST_BUSY, start_ist="2026-10-06T06:00:00+05:30"), State.EXECUTE, "read_bounds"),
        (_call(LIST_BUSY, start_ist="2026-10-01T13:00:00+05:30"), State.EXECUTE, "read_bounds"),
        (_call(LIST_BUSY, start_ist="not a date"), State.EXECUTE, "read_bounds"),
    ],
)
def test_gate_rejections(gate: ToolGate, call: ProposedCall, state: State, rule: str) -> None:
    decision = gate.check(call, state=state, expected=EXPECTED)
    assert not decision.approved and decision.rule == rule


def test_cap_and_once(gate: ToolGate) -> None:
    assert (
        gate.check(_call(LIST_BUSY), state=State.EXECUTE, expected=EXPECTED, call_index=4).rule
        == "cap"
    )
    once = gate.check(_call(CREATE_HOLD), state=State.EXECUTE, expected=EXPECTED, already_done=True)
    assert once.rule == "once"


def test_defaults_and_float_ints_are_normalised(gate: ToolGate) -> None:
    args = {k: v for k, v in EXPECTED[CREATE_HOLD].items() if k not in ("kind", "version")}
    assert gate.check(
        ProposedCall(CREATE_HOLD, args), state=State.EXECUTE, expected=EXPECTED
    ).approved
    floaty = _call(CREATE_HOLD, version=1.0)  # Gemini returns JSON numbers as floats
    assert gate.check(floaty, state=State.EXECUTE, expected=EXPECTED).approved


def test_list_busy_allowed_while_offering(gate: ToolGate) -> None:
    assert gate.check(_call(LIST_BUSY), state=State.COLLECT_PREF, expected=EXPECTED).approved


def test_check_job(gate: ToolGate) -> None:
    assert gate.check_job(_call(CREATE_HOLD)).approved
    assert gate.check_job(_call(LIST_BUSY)).rule == "unknown_tool"
    assert gate.check_job(_call(GMAIL_DRAFT, slot="+919876543210")).rule == "pii"


# --- Runner ------------------------------------------------------------------------------


class RecordingCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        return {"ok": True}


def _agent(proposals: list[tuple[str, dict]] | Exception) -> GeminiToolAgent:
    async def generate(system, facts_json, decls, allowed):  # type: ignore[no-untyped-def]
        assert "NL-A742" in facts_json and "+91" not in facts_json
        if isinstance(proposals, Exception):
            raise proposals
        return proposals

    async def declarations():  # type: ignore[no-untyped-def]
        return [{"name": t, "description": "", "parameters": {}} for t in EXPECTED]

    return GeminiToolAgent(generate, declarations, timeout_s=1)


def _runner(gate: ToolGate, agent: GeminiToolAgent | None) -> GatedToolRunner:
    return GatedToolRunner(RecordingCaller(), gate, agent, fixed_clock)


async def test_llm_proposals_approved(gate: ToolGate) -> None:
    runner = _runner(gate, _agent([(t, dict(a)) for t, a in EXPECTED.items()]))
    approved = await runner.plan(STEP, State.EXECUTE)
    assert list(approved) == list(EXPECTED)
    assert {d["source"] for d in runner.decisions} == {"llm"}


async def test_llm_proposals_in_any_order_are_reordered(gate: ToolGate) -> None:
    runner = _runner(gate, _agent([(t, dict(a)) for t, a in reversed(EXPECTED.items())]))
    assert list(await runner.plan(STEP, State.EXECUTE)) == list(EXPECTED)


@pytest.mark.parametrize(
    "proposals",
    [
        [(CREATE_HOLD, {**EXPECTED[CREATE_HOLD], "code": "NL-Z999"})],  # hallucinated value
        [(t, dict(a)) for t, a in EXPECTED.items() if t != GMAIL_DRAFT],  # incomplete
        [(t, dict(a)) for t, a in EXPECTED.items()] + [(CREATE_HOLD, EXPECTED[CREATE_HOLD])],
        [("gmail_send", {})],
        RuntimeError("quota"),
    ],
)
async def test_bad_or_failed_llm_falls_back_to_plan(gate: ToolGate, proposals: object) -> None:
    runner = _runner(gate, _agent(proposals))  # type: ignore[arg-type]
    approved = await runner.plan(STEP, State.EXECUTE)
    assert {t: c.args for t, c in approved.items()} == EXPECTED
    assert runner.decisions[-1]["source"] == "fallback"


async def test_no_agent_uses_fallback(gate: ToolGate) -> None:
    approved = await _runner(gate, None).plan(STEP, State.EXECUTE)
    assert list(approved) == list(EXPECTED)


async def test_fallback_rejected_raises(gate: ToolGate) -> None:
    with pytest.raises(ToolPolicyError):
        await _runner(gate, None).plan(STEP, State.CONFIRM_SLOT)  # writes before confirm


async def test_call_records(gate: ToolGate) -> None:
    runner = _runner(gate, None)
    await runner.call(ProposedCall(LIST_BUSY, EXPECTED[LIST_BUSY]))
    assert runner.client.calls == [(LIST_BUSY, EXPECTED[LIST_BUSY])]  # type: ignore[attr-defined]
    assert runner.calls[-1]["tool"] == LIST_BUSY
