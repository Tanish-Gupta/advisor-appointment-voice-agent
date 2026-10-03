"""Booking service (LLD 4.1, 5.1, 6.1, 6.2) - the plan's ExecuteBookingSideEffects.

The only code that creates, moves or cancels bookings. It never calls Google: in ONE store
transaction it updates slot reservations and the bookings row and enqueues the gated MCP write
calls as outbox jobs with deterministic idempotency keys `{code}:{tool}:{tag}` (tag = the
booking version, or `cancel`). The outbox worker (`mcp_client/outbox.py`) executes them through
the real FastMCP Google tools.

Storage is behind the `BookingStore` protocol so the domain stays free of I/O imports.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from advisor_agent.domain import codes
from advisor_agent.domain.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Preference,
    Slot,
    Topic,
)
from advisor_agent.domain.plan import (
    BookingPlan,
    CancelPlan,
    ReschedulePlan,
    ToolPlan,
    WaitlistPlan,
    hold_event_id,
)
from advisor_agent.domain.slots import SlotRepository
from advisor_agent.domain.timeutil import fmt_preference, fmt_slot, ist_datetime, to_ist

Clock = Callable[[], datetime]


class InvalidBookingState(Exception):
    pass


@dataclass(frozen=True)
class NewJob:
    idempotency_key: str
    booking_code: str
    tool: str
    args: dict[str, Any]


class BookingTx(Protocol):
    def code_exists(self, code: str) -> bool: ...
    def reserve_slot(self, slot_id: str, code: str) -> None: ...  # raises SlotTaken
    def release_slot(self, slot_id: str) -> None: ...
    def insert_booking(self, booking: Booking, session_id: str, now: datetime) -> None: ...
    def move_booking(self, code: str, slot: Slot, version: int, now: datetime) -> None: ...
    def set_booking_status(self, code: str, status: BookingStatus, now: datetime) -> None: ...
    def enqueue(self, job: NewJob, now: datetime) -> None: ...


class BookingStore(Protocol):
    def transaction(self) -> AbstractContextManager[BookingTx]: ...
    def code_exists(self, code: str) -> bool: ...
    def get_booking(self, code: str) -> Booking | None: ...
    def active_booking_for_session(self, session_id: str) -> Booking | None: ...


def job_key(code: str, tool: str, tag: int | str) -> str:
    return f"{code}:{tool}:{tag}"


# Statuses a booking can be rescheduled or cancelled from.
MOVABLE = frozenset({BookingStatus.TENTATIVE, BookingStatus.NEEDS_ATTENTION})
CANCELLABLE = MOVABLE | {BookingStatus.WAITLIST}


class BookingService:
    def __init__(
        self,
        store: BookingStore,
        slots: SlotRepository,
        clock: Clock,
        *,
        slot_minutes: int = 30,
        business_hours: tuple[int, int] = (9, 18),
        waitlist_hold: bool = True,  # AGENT_WAITLIST_MODE=hold (False => notes_only)
        cancel_draft: bool = True,  # AGENT_CANCEL_DRAFT_ENABLED
    ) -> None:
        self._store = store
        self._slots = slots
        self._clock = clock
        self._slot_minutes = slot_minutes
        self._open_hour = business_hours[0]
        self.waitlist_hold = waitlist_hold
        self.cancel_draft = cancel_draft

    def new_code(self, kind: BookingKind = BookingKind.BOOKING) -> str:
        return codes.generate(kind, self._store.code_exists)

    def today_ist(self) -> date:
        return to_ist(self._clock()).date()

    def plan(self, topic: Topic, slot: Slot) -> BookingPlan:
        return BookingPlan(code=self.new_code(), topic=topic, slot=slot, booked_on=self.today_ist())

    def get(self, code: str) -> Booking | None:
        return self._store.get_booking(code)

    def active_for_session(self, session_id: str) -> Booking | None:
        return self._store.active_booking_for_session(session_id)

    def create(
        self, session_id: str, plan: BookingPlan, calls: Mapping[str, Mapping[str, Any]]
    ) -> Booking:
        """Persist a tentative booking and enqueue its write jobs (all or nothing).

        `calls` are the ToolGate-approved arguments per write tool. Idempotent per session:
        a second confirm in the same session returns the existing booking.
        Raises SlotTaken if the slot was reserved in the meantime.
        """
        existing = self._store.active_booking_for_session(session_id)
        if existing is not None:
            return existing
        if plan.kind is not BookingKind.BOOKING:
            raise InvalidBookingState("create() books slots; use create_waitlist()")
        _require(plan, calls)

        booking = Booking(
            code=plan.code,
            kind=plan.kind,
            topic=plan.topic,
            slot=plan.slot,
            preference=None,
            status=BookingStatus.TENTATIVE,
        )
        now = self._clock()
        self._slots.reserve(plan.slot.slot_id, plan.code)  # picker view; raises SlotTaken
        try:
            with self._store.transaction() as tx:
                tx.reserve_slot(plan.slot.slot_id, plan.code)  # DB-level guarantee
                tx.insert_booking(booking, session_id, now)
                _enqueue(tx, plan, calls, now)
        except BaseException:
            self._slots.release(plan.slot.slot_id)
            raise
        return booking

    # --- waitlist (LLD 5.1) --------------------------------------------------------------

    def plan_waitlist(self, topic: Topic, pref: Preference) -> WaitlistPlan:
        """Waitlist plan for a preference no slot matched.

        In `hold` mode the transparent marker hold sits at the start of the requested window
        (or opening time) on the requested day; without a day, or if that moment has passed,
        the plan is notes-only.
        """
        start = end = None
        if self.waitlist_hold and pref.date is not None:
            at = pref.window.start if pref.window else time(self._open_hour)
            candidate = ist_datetime(pref.date, at)
            if candidate > self._clock():
                start, end = candidate, candidate + timedelta(minutes=self._slot_minutes)
        return WaitlistPlan(
            code=self.new_code(BookingKind.WAITLIST),
            topic=topic,
            pref_label=fmt_preference(pref),
            booked_on=self.today_ist(),
            hold_start=start,
            hold_end=end,
        )

    def create_waitlist(
        self, session_id: str, plan: WaitlistPlan, calls: Mapping[str, Mapping[str, Any]]
    ) -> Booking:
        existing = self._store.active_booking_for_session(session_id)
        if existing is not None:
            return existing
        _require(plan, calls)
        booking = Booking(
            code=plan.code,
            kind=BookingKind.WAITLIST,
            topic=plan.topic,
            slot=None,
            preference=None,
            status=BookingStatus.WAITLIST,
            pref_label=plan.pref_label,
        )
        now = self._clock()
        with self._store.transaction() as tx:
            tx.insert_booking(booking, session_id, now)
            _enqueue(tx, plan, calls, now)
        return booking

    # --- reschedule (LLD 6.1) ------------------------------------------------------------

    def plan_reschedule(self, booking: Booking, slot: Slot) -> ReschedulePlan:
        if booking.kind is not BookingKind.BOOKING or booking.status not in MOVABLE:
            raise InvalidBookingState(f"{booking.code} cannot be rescheduled ({booking.status})")
        return ReschedulePlan(
            code=booking.code,
            topic=booking.topic,
            slot=slot,
            old_event_id=hold_event_id(booking.code, booking.kind.value, booking.version),
            booked_on=self.today_ist(),
            version=booking.version + 1,
        )

    def reschedule(self, plan: ReschedulePlan, calls: Mapping[str, Mapping[str, Any]]) -> Booking:
        """Move the booking to the plan's slot (same code, version+1) and enqueue the jobs.

        Raises SlotTaken if the new slot is gone, InvalidBookingState if the booking can no
        longer be moved (cancelled meanwhile, or already moved to this version).
        """
        booking = self._store.get_booking(plan.code)
        if booking is None or booking.status not in MOVABLE or booking.version + 1 != plan.version:
            raise InvalidBookingState(f"{plan.code} cannot be rescheduled now")
        _require(plan, calls)
        old_slot = booking.slot
        now = self._clock()
        self._slots.reserve(plan.slot.slot_id, plan.code)  # raises SlotTaken
        try:
            with self._store.transaction() as tx:
                tx.reserve_slot(plan.slot.slot_id, plan.code)
                if old_slot is not None:
                    tx.release_slot(old_slot.slot_id)
                tx.move_booking(plan.code, plan.slot, plan.version, now)
                _enqueue(tx, plan, calls, now)
        except BaseException:
            self._slots.release(plan.slot.slot_id)
            raise
        if old_slot is not None:
            self._slots.release(old_slot.slot_id)
        moved = self._store.get_booking(plan.code)
        assert moved is not None
        return moved

    # --- cancel (LLD 6.2) ----------------------------------------------------------------

    def plan_cancel(self, booking: Booking) -> CancelPlan:
        if booking.status not in CANCELLABLE:
            raise InvalidBookingState(f"{booking.code} cannot be cancelled ({booking.status})")
        label = fmt_slot(booking.slot) if booking.slot else (booking.pref_label or "waitlist")
        return CancelPlan(
            code=booking.code,
            topic=booking.topic,
            event_id=hold_event_id(booking.code, booking.kind.value, booking.version),
            slot_label=label,
            booked_on=self.today_ist(),
            send_draft=self.cancel_draft,
        )

    def cancel(self, plan: CancelPlan, calls: Mapping[str, Mapping[str, Any]]) -> Booking:
        booking = self._store.get_booking(plan.code)
        if booking is None or booking.status not in CANCELLABLE:
            raise InvalidBookingState(f"{plan.code} cannot be cancelled now")
        _require(plan, calls)
        now = self._clock()
        with self._store.transaction() as tx:
            tx.set_booking_status(plan.code, BookingStatus.CANCELLED, now)
            if booking.slot is not None:
                tx.release_slot(booking.slot.slot_id)
            _enqueue(tx, plan, calls, now)
        if booking.slot is not None:
            self._slots.release(booking.slot.slot_id)
        return booking.model_copy(update={"status": BookingStatus.CANCELLED})


def _require(plan: ToolPlan, calls: Mapping[str, Mapping[str, Any]]) -> None:
    missing = [t for t in plan.writes if t not in calls]
    if missing:
        raise ValueError(f"approved calls missing for {missing}")


def _enqueue(
    tx: BookingTx, plan: ToolPlan, calls: Mapping[str, Mapping[str, Any]], now: datetime
) -> None:
    for tool in plan.writes:
        tx.enqueue(
            NewJob(
                idempotency_key=job_key(plan.code, tool, plan.job_tag),
                booking_code=plan.code,
                tool=tool,
                args=dict(calls[tool]),
            ),
            now,
        )
