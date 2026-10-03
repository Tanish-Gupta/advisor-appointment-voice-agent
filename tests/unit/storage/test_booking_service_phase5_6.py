"""BookingService Phase 5/6: waitlist, reschedule and cancel plans + transactions."""

from datetime import date, datetime, time

import pytest

from advisor_agent.domain.booking_service import BookingService, InvalidBookingState, job_key
from advisor_agent.domain.models import BookingKind, BookingStatus, Preference, TimeWindow, Topic
from advisor_agent.domain.plan import (
    BOOKING_WRITES,
    CREATE_HOLD,
    DELETE_HOLD,
    DOCS_APPEND,
    GMAIL_DRAFT,
    hold_event_id,
)
from advisor_agent.domain.slots import InMemorySlotRepository, SlotTaken, make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.storage.sqlite import SqliteStore
from tests.conftest import fixed_clock

SLOT = make_slot(datetime(2026, 10, 6, 13, 0, tzinfo=IST))
OTHER = make_slot(datetime(2026, 10, 7, 10, 0, tzinfo=IST))
AFTERNOON = TimeWindow(start=time(12), end=time(16), label="afternoon")


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def repo() -> InMemorySlotRepository:
    return InMemorySlotRepository([SLOT, OTHER])


def _svc(store, repo, **kw):  # type: ignore[no-untyped-def]
    return BookingService(store, repo, fixed_clock, **kw)


def _all(plan):  # type: ignore[no-untyped-def]
    calls = plan.expected_calls()
    return {t: calls[t] for t in plan.writes}


def _booked(svc: BookingService) -> str:
    plan = svc.plan(Topic.SIP_MANDATES, SLOT)
    calls = plan.expected_calls()
    svc.create("s1", plan, {t: calls[t] for t in BOOKING_WRITES})
    return plan.code


# --- waitlist ---------------------------------------------------------------------------------


def test_waitlist_hold_at_window_start(store, repo) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(store, repo)
    plan = svc.plan_waitlist(Topic.KYC_ONBOARDING, Preference(date=date(2026, 10, 6),
                                                              window=AFTERNOON))  # fmt: skip
    assert plan.writes == (CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT)
    assert plan.hold_start == datetime(2026, 10, 6, 12, 0, tzinfo=IST)
    calls = plan.expected_calls()
    assert calls[CREATE_HOLD]["kind"] == "waitlist"
    assert calls[DOCS_APPEND]["status"] == "waitlist"
    assert calls[GMAIL_DRAFT]["template"] == "waitlist"
    assert calls[DOCS_APPEND]["slot"] == "Tuesday, 6 October 2026 (afternoon)"

    booking = svc.create_waitlist("s1", plan, _all(plan))
    assert booking.kind is BookingKind.WAITLIST and booking.status is BookingStatus.WAITLIST
    assert store.get_booking(plan.code).pref_label == "Tuesday, 6 October 2026 (afternoon)"
    assert store.reserved_slot_ids() == []
    assert [j.tool for j in store.jobs(plan.code)] == list(plan.writes)


@pytest.mark.parametrize(
    ("kw", "pref"),
    [
        ({"waitlist_hold": False}, Preference(date=date(2026, 10, 6))),
        ({}, Preference()),  # no day: nothing to anchor the hold to
        ({}, Preference(date=date(2026, 10, 2))),  # 9 AM today has already passed (16:00 IST)
    ],
)
def test_waitlist_notes_only(store, repo, kw, pref) -> None:  # type: ignore[no-untyped-def]
    plan = _svc(store, repo, **kw).plan_waitlist(Topic.SIP_MANDATES, pref)
    assert plan.writes == (DOCS_APPEND, GMAIL_DRAFT) and plan.hold_start is None


# --- reschedule -------------------------------------------------------------------------------


def test_reschedule_moves_slot_and_bumps_version(store, repo) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(store, repo)
    code = _booked(svc)
    plan = svc.plan_reschedule(store.get_booking(code), OTHER)
    assert plan.reads == ()
    assert plan.writes == (CREATE_HOLD, DELETE_HOLD, DOCS_APPEND, GMAIL_DRAFT)
    calls = plan.expected_calls()
    assert calls[DELETE_HOLD]["event_id"] == hold_event_id(code, "booking", 1)
    assert calls[CREATE_HOLD]["version"] == 2
    assert calls[DOCS_APPEND]["status"] == "rescheduled"
    assert calls[GMAIL_DRAFT]["template"] == "reschedule"

    svc.reschedule(plan, _all(plan))
    booking = store.get_booking(code)
    assert booking.slot == OTHER and booking.version == 2
    assert booking.status is BookingStatus.TENTATIVE and booking.calendar_event_id is None
    assert store.reserved_slot_ids() == [OTHER.slot_id]
    assert SLOT in repo.free_slots(SLOT.start_utc, SLOT.end_utc)
    keys = [j.idempotency_key for j in store.jobs(code)][3:]
    assert keys == [job_key(code, t, 2) for t in plan.writes]


def test_reschedule_to_taken_slot_changes_nothing(store, repo) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(store, repo)
    code = _booked(svc)
    repo.reserve(OTHER.slot_id, "NL-X999")
    plan = svc.plan_reschedule(store.get_booking(code), OTHER)
    with pytest.raises(SlotTaken):
        svc.reschedule(plan, _all(plan))
    assert store.get_booking(code).slot == SLOT and len(store.jobs(code)) == 3


def test_waitlist_cannot_be_rescheduled(store, repo) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(store, repo)
    plan = svc.plan_waitlist(Topic.SIP_MANDATES, Preference())
    booking = svc.create_waitlist("s1", plan, _all(plan))
    with pytest.raises(InvalidBookingState):
        svc.plan_reschedule(booking, OTHER)


# --- cancel -----------------------------------------------------------------------------------


@pytest.mark.parametrize("draft", [True, False])
def test_cancel_releases_slot_and_enqueues(store, repo, draft) -> None:  # type: ignore[no-untyped-def]
    svc = _svc(store, repo, cancel_draft=draft)
    code = _booked(svc)
    plan = svc.plan_cancel(store.get_booking(code))
    expected = (DELETE_HOLD, DOCS_APPEND, GMAIL_DRAFT) if draft else (DELETE_HOLD, DOCS_APPEND)
    assert plan.writes == expected
    svc.cancel(plan, _all(plan))
    assert store.get_booking(code).status is BookingStatus.CANCELLED
    assert store.reserved_slot_ids() == []
    assert [j.idempotency_key for j in store.jobs(code)][3:] == [
        job_key(code, t, "cancel") for t in expected
    ]
    with pytest.raises(InvalidBookingState):
        svc.plan_cancel(store.get_booking(code))
