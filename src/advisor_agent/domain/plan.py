"""Tool plans (LLD 3.11, 5.1, 6.1, 6.2): the exact MCP tool calls each side effect needs.

Pure data. The ToolGate compares every proposed call (from Gemini or the fallback) against
`expected_calls()`; the BookingService turns the approved write calls into outbox jobs keyed
`{code}:{tool}:{job_tag}`.

* BookingPlan    - book_new: list busy, create hold, notes line, advisor draft.
* WaitlistPlan   - no slot matched: (optional) transparent waitlist hold, notes line, draft.
* ReschedulePlan - same code, version+1: create the new hold, then delete the old one.
* CancelPlan     - delete the hold, notes line, (optional) draft.
"""

import base64
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar, Protocol

from advisor_agent.domain.models import BookingKind, Slot, Topic
from advisor_agent.domain.timeutil import fmt_slot, to_ist

LIST_BUSY = "calendar_list_busy"
CREATE_HOLD = "calendar_create_hold"
DELETE_HOLD = "calendar_delete_hold"
DOCS_APPEND = "docs_append_prebooking"
GMAIL_DRAFT = "gmail_create_draft"

READ_TOOLS = frozenset({LIST_BUSY})
WRITE_TOOLS = frozenset({CREATE_HOLD, DELETE_HOLD, DOCS_APPEND, GMAIL_DRAFT})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS

# Write calls of a new booking, in the order the outbox must run them.
BOOKING_WRITES = (CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT)


def hold_event_id(code: str, kind: str, version: int) -> str:
    """Deterministic Google Calendar event id per `{code}:{kind}:{version}`.

    Google event ids allow base32hex chars (0-9, a-v), 5-1024 long. Plans use it to address the
    hold to delete (reschedule/cancel) without waiting for the create job's result.
    """
    digest = hashlib.sha256(f"{code}:{kind}:{version}".encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=")[:26].lower()


class ToolPlan(Protocol):
    """What the executor and BookingService need from any plan."""

    @property
    def code(self) -> str: ...
    @property
    def reads(self) -> tuple[str, ...]: ...  # inline read-only calls (before the writes)
    @property
    def writes(self) -> tuple[str, ...]: ...  # outbox jobs, in execution order
    @property
    def job_tag(self) -> str: ...  # idempotency-key suffix
    def expected_calls(self) -> dict[str, dict[str, Any]]: ...
    def facts(self) -> dict[str, Any]: ...


def ist_iso(dt: datetime) -> str:
    """'2026-10-06T13:00:00+05:30' - the only datetime format passed to the MCP tools."""
    return to_ist(dt).replace(microsecond=0).isoformat()


def list_busy_args(start: datetime, end: datetime) -> dict[str, Any]:
    return {"start_ist": ist_iso(start), "end_ist": ist_iso(end)}


@dataclass(frozen=True)
class BookingPlan:
    code: str
    topic: Topic
    slot: Slot
    booked_on: date  # IST date the booking was made (the "date" column in Advisor Pre-Bookings)
    kind: BookingKind = BookingKind.BOOKING
    version: int = 1

    reads: ClassVar[tuple[str, ...]] = (LIST_BUSY,)
    writes: ClassVar[tuple[str, ...]] = BOOKING_WRITES

    @property
    def job_tag(self) -> str:
        return str(self.version)

    @property
    def start_ist(self) -> str:
        return ist_iso(self.slot.start_utc)

    @property
    def end_ist(self) -> str:
        return ist_iso(self.slot.end_utc)

    @property
    def slot_label(self) -> str:
        return fmt_slot(self.slot)

    @property
    def status(self) -> str:
        return "tentative" if self.kind is BookingKind.BOOKING else "waitlist"

    def expected_calls(self) -> dict[str, dict[str, Any]]:
        """Tool name -> exact arguments. Insertion order is the execution order."""
        return {
            LIST_BUSY: list_busy_args(self.slot.start_utc, self.slot.end_utc),
            CREATE_HOLD: {
                "code": self.code,
                "topic": self.topic.value,
                "start_ist": self.start_ist,
                "end_ist": self.end_ist,
                "kind": self.kind.value,
                "version": self.version,
            },
            DOCS_APPEND: {
                "date": self.booked_on.isoformat(),
                "topic": self.topic.value,
                "slot": self.slot_label,
                "code": self.code,
                "status": self.status,
            },
            GMAIL_DRAFT: {
                "template": self.kind.value,
                "code": self.code,
                "topic": self.topic.value,
                "slot": self.slot_label,
            },
        }

    def facts(self) -> dict[str, Any]:
        """PII-free facts handed to the Gemini tool agent (LLD 3.10)."""
        return {
            "task": (
                "A client confirmed a tentative advisor slot. Check the slot is still free on the "
                "advisor calendar, create the tentative calendar hold, append the pre-booking "
                "line to the notes document, and prepare the advisor email draft."
            ),
            "booking_code": self.code,
            "topic": self.topic.value,
            "kind": self.kind.value,
            "version": self.version,
            "slot_start_ist": self.start_ist,
            "slot_end_ist": self.end_ist,
            "slot_label": self.slot_label,
            "booked_on": self.booked_on.isoformat(),
            "notes_status": self.status,
            "email_template": self.kind.value,
        }


@dataclass(frozen=True)
class WaitlistPlan:
    """No slot matched (LLD 5.1). `hold_start/hold_end` None => notes-only (no calendar job)."""

    code: str
    topic: Topic
    pref_label: str  # the requested day/window, e.g. 'Tuesday, 6 October 2026 (afternoon)'
    booked_on: date
    hold_start: datetime | None = None
    hold_end: datetime | None = None
    version: int = 1

    reads: ClassVar[tuple[str, ...]] = ()

    @property
    def with_hold(self) -> bool:
        return self.hold_start is not None and self.hold_end is not None

    @property
    def writes(self) -> tuple[str, ...]:
        return BOOKING_WRITES if self.with_hold else (DOCS_APPEND, GMAIL_DRAFT)

    @property
    def job_tag(self) -> str:
        return str(self.version)

    def expected_calls(self) -> dict[str, dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        if self.hold_start is not None and self.hold_end is not None:
            calls[CREATE_HOLD] = {
                "code": self.code,
                "topic": self.topic.value,
                "start_ist": ist_iso(self.hold_start),
                "end_ist": ist_iso(self.hold_end),
                "kind": BookingKind.WAITLIST.value,
                "version": self.version,
            }
        calls[DOCS_APPEND] = {
            "date": self.booked_on.isoformat(),
            "topic": self.topic.value,
            "slot": self.pref_label,
            "code": self.code,
            "status": "waitlist",
        }
        calls[GMAIL_DRAFT] = {
            "template": "waitlist",
            "code": self.code,
            "topic": self.topic.value,
            "slot": self.pref_label,
        }
        return calls

    def facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "task": (
                "No advisor slot matched the client's preference, so they joined the waitlist. "
                + (
                    "Create the waitlist calendar hold (kind waitlist), "
                    if self.with_hold
                    else "Do not create a calendar hold. "
                )
                + "append the waitlist line to the notes document and prepare the advisor "
                "email draft (template waitlist)."
            ),
            "booking_code": self.code,
            "topic": self.topic.value,
            "kind": BookingKind.WAITLIST.value,
            "version": self.version,
            "preference_label": self.pref_label,
            "booked_on": self.booked_on.isoformat(),
            "notes_status": "waitlist",
            "email_template": "waitlist",
        }
        if self.hold_start is not None and self.hold_end is not None:
            facts["hold_start_ist"] = ist_iso(self.hold_start)
            facts["hold_end_ist"] = ist_iso(self.hold_end)
        return facts


@dataclass(frozen=True)
class ReschedulePlan:
    """Same code, version+1 (LLD 6.1). The new hold is created BEFORE the old one is deleted;
    the outbox runs a booking's jobs in order and skips the delete if the create died."""

    code: str
    topic: Topic
    slot: Slot  # the new slot
    old_event_id: str
    booked_on: date
    version: int  # the NEW version

    reads: ClassVar[tuple[str, ...]] = ()  # the executor checks the new slot with list_busy
    writes: ClassVar[tuple[str, ...]] = (CREATE_HOLD, DELETE_HOLD, DOCS_APPEND, GMAIL_DRAFT)

    @property
    def job_tag(self) -> str:
        return str(self.version)

    @property
    def slot_label(self) -> str:
        return fmt_slot(self.slot)

    def expected_calls(self) -> dict[str, dict[str, Any]]:
        return {
            CREATE_HOLD: {
                "code": self.code,
                "topic": self.topic.value,
                "start_ist": ist_iso(self.slot.start_utc),
                "end_ist": ist_iso(self.slot.end_utc),
                "kind": BookingKind.BOOKING.value,
                "version": self.version,
            },
            DELETE_HOLD: {"event_id": self.old_event_id},
            DOCS_APPEND: {
                "date": self.booked_on.isoformat(),
                "topic": self.topic.value,
                "slot": self.slot_label,
                "code": self.code,
                "status": "rescheduled",
            },
            GMAIL_DRAFT: {
                "template": "reschedule",
                "code": self.code,
                "topic": self.topic.value,
                "slot": self.slot_label,
            },
        }

    def facts(self) -> dict[str, Any]:
        return {
            "task": (
                "A client moved their tentative advisor booking to a new slot. Create the new "
                "tentative calendar hold, delete the old hold, append the rescheduled line to "
                "the notes document and prepare the advisor email draft (template reschedule)."
            ),
            "booking_code": self.code,
            "topic": self.topic.value,
            "kind": BookingKind.BOOKING.value,
            "version": self.version,
            "slot_start_ist": ist_iso(self.slot.start_utc),
            "slot_end_ist": ist_iso(self.slot.end_utc),
            "slot_label": self.slot_label,
            "old_event_id": self.old_event_id,
            "booked_on": self.booked_on.isoformat(),
            "notes_status": "rescheduled",
            "email_template": "reschedule",
        }


@dataclass(frozen=True)
class CancelPlan:
    """Cancel (LLD 6.2): delete the hold (idempotent: an absent hold counts as deleted), append
    the cancelled line and, if enabled, the advisor draft. Job keys use the tag `cancel`."""

    code: str
    topic: Topic
    event_id: str
    slot_label: str  # full IST slot, or the waitlist preference label
    booked_on: date
    send_draft: bool = True

    reads: ClassVar[tuple[str, ...]] = ()
    job_tag: ClassVar[str] = "cancel"

    @property
    def writes(self) -> tuple[str, ...]:
        base = (DELETE_HOLD, DOCS_APPEND)
        return (*base, GMAIL_DRAFT) if self.send_draft else base

    def expected_calls(self) -> dict[str, dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {
            DELETE_HOLD: {"event_id": self.event_id},
            DOCS_APPEND: {
                "date": self.booked_on.isoformat(),
                "topic": self.topic.value,
                "slot": self.slot_label,
                "code": self.code,
                "status": "cancelled",
            },
        }
        if self.send_draft:
            calls[GMAIL_DRAFT] = {
                "template": "cancel",
                "code": self.code,
                "topic": self.topic.value,
                "slot": self.slot_label,
            }
        return calls

    def facts(self) -> dict[str, Any]:
        return {
            "task": (
                "A client cancelled their tentative advisor booking. Delete the calendar hold, "
                "append the cancelled line to the notes document"
                + (
                    " and prepare the advisor email draft (template cancel)."
                    if self.send_draft
                    else ". Do not create an email draft."
                )
            ),
            "booking_code": self.code,
            "topic": self.topic.value,
            "event_id": self.event_id,
            "slot_label": self.slot_label,
            "booked_on": self.booked_on.isoformat(),
            "notes_status": "cancelled",
            "email_template": "cancel",
        }
