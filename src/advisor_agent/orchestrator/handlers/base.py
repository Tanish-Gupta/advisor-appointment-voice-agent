"""Shared handler types."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from advisor_agent.domain.models import Booking, Preference, Slot, Topic
from advisor_agent.domain.slots import Clock, SlotPicker
from advisor_agent.nlu.schema import NLUResult
from advisor_agent.orchestrator.context import Flow, FollowUp, SessionContext
from advisor_agent.orchestrator.states import State, assert_transition_allowed


class BookedResult(Protocol):
    @property
    def code(self) -> str: ...
    @property
    def secure_url(self) -> str: ...
    @property
    def ttl_label(self) -> str: ...


class BookingExecutor(Protocol):
    """Side effects behind EXECUTE (implemented by mcp_client.McpBookingExecutor).

    Phase 4: busy check + book. Phase 5: waitlist. Phase 6: lookup, reschedule, cancel.
    """

    async def busy_slot_ids(self, slots: list[Slot], state: State) -> set[str]: ...
    async def book(self, session_id: str, topic: Topic, slot: Slot) -> BookedResult: ...
    async def waitlist(self, session_id: str, topic: Topic, pref: Preference) -> BookedResult: ...
    async def lookup(self, code: str) -> Booking | None: ...
    async def reschedule(self, code: str, slot: Slot) -> BookedResult: ...
    async def cancel(self, code: str) -> Booking: ...


@dataclass(frozen=True)
class Deps:
    picker: SlotPicker
    clock: Clock
    horizon_days: int = 14
    stub_hints: bool = True  # add STUB_HELP to clarifications while the stub NLU is active
    confidence_threshold: float = 0.7  # AGENT_NLU_CONFIDENCE_THRESHOLD (LLD 3.7)
    business_hours: tuple[int, int] = (9, 18)
    executor: BookingExecutor | None = None  # None => MCP tools disabled (EXECUTION_NOT_ENABLED)


@dataclass
class Outcome:
    next_state: State
    templates: list[str]
    params: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[SessionContext, NLUResult, Deps], Awaitable[Outcome]]


def offer_templates(slots: list[Slot]) -> list[str]:
    return ["OFFER_TWO"] if len(slots) >= 2 else ["OFFER_ONE"]


def enter(ctx: SessionContext, state: State) -> None:
    """Move to `state` mid-turn (a handler delegating to another state's handler)."""
    assert_transition_allowed(ctx.state, state, disclaimer_acknowledged=ctx.disclaimer_acknowledged)
    ctx.state = state


def confirm_templates(ctx: SessionContext) -> list[str]:
    return ["RESCHEDULE_READBACK"] if ctx.flow is Flow.RESCHEDULE else ["CONFIRM_READBACK"]


def cancel_readback(ctx: SessionContext) -> list[str]:
    return ["CANCEL_READBACK"] if ctx.current_slot is not None else ["CANCEL_READBACK_WAITLIST"]


def followup_templates(ctx: SessionContext) -> list[str]:
    return ["OFFER_TO_BOOK"] if ctx.followup is FollowUp.BOOK else ["ANYTHING_ELSE"]


def reprompt(ctx: SessionContext, state: State | None = None) -> list[str]:
    """The question the user is currently expected to answer (in `state`, default ctx.state)."""
    match state or ctx.state:
        case State.DISCLAIMER_ACK:
            return ["DISCLAIMER_ASK_ACK"]
        case State.INTENT_DETECT | State.CLARIFY:
            return followup_templates(ctx) if ctx.followup else ["CLARIFY_INTENT"]
        case State.TOPIC_CONFIRM:
            return ["ASK_TOPIC"]
        case State.COLLECT_PREF:
            return ["ASK_PREF"]
        case State.OFFER_SLOTS:
            return offer_templates(ctx.offered_slots)
        case State.CONFIRM_SLOT:
            return confirm_templates(ctx)
        case State.ASK_CODE:
            return ["ASK_CODE_REPROMPT"]
        case State.CANCEL_CONFIRM:
            return cancel_readback(ctx)
        case State.PREP_TOPIC:
            return ["PREP_ASK_TOPIC"]
        case State.AVAILABILITY:
            return ["ASK_DAY_FOR_AVAILABILITY"]
        case State.AWAIT_PIVOT:
            return ["PIVOT_REPROMPT"]
        case _:
            return []
