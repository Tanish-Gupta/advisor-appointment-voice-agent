"""McpBookingExecutor (LLD 3.12 / 4.1): what EXECUTE does after the user says "yes".

1. Plan the booking (code + exact calls) with the BookingService.
2. The Gemini tool agent proposes the MCP calls; the ToolGate approves them (or the
   deterministic fallback is used).
3. `calendar_list_busy` runs inline through MCP: if the advisor calendar is busy at that time,
   raise SlotTaken so the conversation offers other slots.
4. The approved write calls are persisted with the booking as outbox jobs in one transaction;
   the worker executes them through the real Google tools.
5. A signed single-use secure link is issued for the contact details.

Phase 5/6 add the same pattern for the waitlist (no inline read), reschedule (the new slot is
checked with `calendar_list_busy`, then create-new / delete-old) and cancel.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from advisor_agent.domain.booking_service import BookingService
from advisor_agent.domain.models import Booking, BookingStatus, Preference, Slot, Topic
from advisor_agent.domain.plan import BOOKING_WRITES, LIST_BUSY, ToolPlan, list_busy_args
from advisor_agent.domain.slots import SlotTaken
from advisor_agent.mcp_client.client import ToolCallError
from advisor_agent.mcp_client.runner import GatedToolRunner
from advisor_agent.nlu.tool_agent import AgentStep
from advisor_agent.orchestrator.states import State

log = logging.getLogger(__name__)


class LinkIssuer(Protocol):
    @property
    def ttl_label(self) -> str: ...
    def issue(self, code: str) -> str: ...


@dataclass(frozen=True)
class BookingResult:
    code: str
    secure_url: str
    ttl_label: str


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def busy_overlaps(slots: Iterable[Slot], busy: Iterable[dict[str, Any]]) -> set[str]:
    intervals = []
    for b in busy:
        try:
            intervals.append((_parse(str(b["start"])), _parse(str(b["end"]))))
        except (KeyError, ValueError):
            continue
    return {
        s.slot_id
        for s in slots
        if any(s.start_utc < end and start < s.end_utc for start, end in intervals)
    }


class McpBookingExecutor:
    def __init__(
        self,
        runner: GatedToolRunner,
        bookings: BookingService,
        links: LinkIssuer,
        *,
        on_enqueued: Callable[[], None] | None = None,
    ) -> None:
        self._runner = runner
        self._bookings = bookings
        self._links = links
        self._on_enqueued = on_enqueued

    async def busy_slot_ids(self, slots: list[Slot], state: State) -> set[str]:
        """Slots that overlap a busy interval on the advisor calendar (read-only MCP call).

        Uses the deterministic plan (no LLM round trip) to keep offering fast; raises
        ToolCallError on failure so the caller can fail open.
        """
        if not slots:
            return set()
        args = list_busy_args(min(s.start_utc for s in slots), max(s.end_utc for s in slots))
        step = AgentStep(
            allowed_tools=(LIST_BUSY,),
            facts={"task": "Check the advisor calendar is free", **args},
            expected={LIST_BUSY: args},
        )
        approved = await self._runner.plan(step, state, use_agent=False)
        result = await self._runner.call(approved[LIST_BUSY])
        return busy_overlaps(slots, result.get("busy", []))

    async def book(self, session_id: str, topic: Topic, slot: Slot) -> BookingResult:
        existing = self._bookings.active_for_session(session_id)
        if existing is not None:  # idempotent: a repeated "yes" never books twice
            return self._result(existing.code)

        plan = self._bookings.plan(topic, slot)
        step = AgentStep(
            allowed_tools=(LIST_BUSY, *BOOKING_WRITES),
            facts=plan.facts(),
            expected=plan.expected_calls(),
        )
        approved = await self._runner.plan(step, State.EXECUTE)

        try:
            busy = (await self._runner.call(approved[LIST_BUSY])).get("busy", [])
        except ToolCallError as e:  # fail open: the hold itself is idempotent and reviewable
            log.warning(
                "list_busy failed before booking; continuing", extra={"error": str(e)[:200]}
            )
            busy = []
        if busy_overlaps([slot], busy):
            raise SlotTaken(slot.slot_id)

        writes = {tool: approved[tool].args for tool in BOOKING_WRITES}
        booking = self._bookings.create(session_id, plan, writes)
        if self._on_enqueued is not None:
            self._on_enqueued()
        return self._result(booking.code)

    # --- Phase 5/6 ------------------------------------------------------------------------

    async def _approved_writes(self, plan: ToolPlan) -> dict[str, dict[str, Any]]:
        step = AgentStep(
            allowed_tools=(*plan.reads, *plan.writes),
            facts=plan.facts(),
            expected=plan.expected_calls(),
        )
        approved = await self._runner.plan(step, State.EXECUTE)
        return {tool: approved[tool].args for tool in plan.writes}

    def _wake(self) -> None:
        if self._on_enqueued is not None:
            self._on_enqueued()

    async def lookup(self, code: str) -> Booking | None:
        return self._bookings.get(code)

    async def waitlist(self, session_id: str, topic: Topic, pref: Preference) -> BookingResult:
        """No slot matched (LLD 5.1): waitlist code, (optional) hold, notes line, draft."""
        existing = self._bookings.active_for_session(session_id)
        if existing is not None:
            return self._result(existing.code)
        plan = self._bookings.plan_waitlist(topic, pref)
        writes = await self._approved_writes(plan)
        booking = self._bookings.create_waitlist(session_id, plan, writes)
        self._wake()
        return self._result(booking.code)

    async def reschedule(self, code: str, slot: Slot) -> BookingResult:
        """Same code, new slot (LLD 6.1). Raises SlotTaken / InvalidBookingState."""
        booking = self._bookings.get(code)
        if booking is None:
            raise LookupError(code)
        if booking.slot is not None and booking.slot.slot_id == slot.slot_id:
            return self._result(code)
        plan = self._bookings.plan_reschedule(booking, slot)
        try:
            busy = await self.busy_slot_ids([slot], State.EXECUTE)
        except ToolCallError as e:
            log.warning("list_busy failed before reschedule; continuing (%s)", str(e)[:200])
            busy = set()
        if busy:
            raise SlotTaken(slot.slot_id)
        writes = await self._approved_writes(plan)
        self._bookings.reschedule(plan, writes)
        self._wake()
        return self._result(code)

    async def cancel(self, code: str) -> Booking:
        """Cancel (LLD 6.2): delete hold, notes line, optional draft; idempotent."""
        booking = self._bookings.get(code)
        if booking is None:
            raise LookupError(code)
        if booking.status is BookingStatus.CANCELLED:
            return booking
        plan = self._bookings.plan_cancel(booking)
        writes = await self._approved_writes(plan)
        cancelled = self._bookings.cancel(plan, writes)
        self._wake()
        return cancelled

    def _result(self, code: str) -> BookingResult:
        return BookingResult(code, self._links.issue(code), self._links.ttl_label)
