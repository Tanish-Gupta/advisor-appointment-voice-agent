"""BookingService (one transaction: slot + booking + outbox jobs) on the real SQLite store."""

from datetime import date, datetime

import pytest

from advisor_agent.domain.booking_service import BookingService, job_key
from advisor_agent.domain.models import BookingStatus, Topic
from advisor_agent.domain.plan import BOOKING_WRITES, CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT
from advisor_agent.domain.slots import InMemorySlotRepository, SlotTaken, make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.storage.sqlite import SqliteStore, sqlite_path
from tests.conftest import fixed_clock

SLOT = make_slot(datetime(2026, 10, 6, 13, 0, tzinfo=IST))
OTHER = make_slot(datetime(2026, 10, 6, 14, 0, tzinfo=IST))


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def repo() -> InMemorySlotRepository:
    return InMemorySlotRepository([SLOT, OTHER])


@pytest.fixture
def svc(store: SqliteStore, repo: InMemorySlotRepository) -> BookingService:
    return BookingService(store, repo, fixed_clock)


def _writes(svc: BookingService, slot=SLOT):  # type: ignore[no-untyped-def]
    plan = svc.plan(Topic.SIP_MANDATES, slot)
    calls = plan.expected_calls()
    return plan, {t: calls[t] for t in BOOKING_WRITES}


def test_plan_uses_ist_today_and_valid_code(svc: BookingService) -> None:
    plan, _ = _writes(svc)
    assert plan.booked_on == date(2026, 10, 2)
    assert plan.code.startswith("NL-") and len(plan.code) == 7


def test_create_persists_booking_slot_and_ordered_jobs(
    svc: BookingService, store: SqliteStore, repo: InMemorySlotRepository
) -> None:
    plan, writes = _writes(svc)
    booking = svc.create("sess-1", plan, writes)
    assert booking.status is BookingStatus.TENTATIVE
    assert store.get_booking(plan.code) == booking
    assert store.reserved_slot_ids() == [SLOT.slot_id]
    jobs = store.jobs(plan.code)
    assert [j.tool for j in jobs] == [CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT]
    assert [j.idempotency_key for j in jobs] == [job_key(plan.code, t, 1) for t in BOOKING_WRITES]
    assert all(j.status == "pending" for j in jobs)
    assert SLOT not in repo.free_slots(SLOT.start_utc, OTHER.end_utc)


def test_create_is_idempotent_per_session(svc: BookingService, store: SqliteStore) -> None:
    plan, writes = _writes(svc)
    first = svc.create("sess-1", plan, writes)
    plan2, writes2 = _writes(svc, OTHER)
    assert svc.create("sess-1", plan2, writes2) == first
    assert len(store.jobs()) == 3
    assert svc.active_for_session("sess-1") == first


def test_slot_taken_rolls_back_everything(
    svc: BookingService, store: SqliteStore, repo: InMemorySlotRepository
) -> None:
    plan, writes = _writes(svc)
    svc.create("sess-1", plan, writes)
    plan2, writes2 = _writes(svc)  # same slot, different session
    with pytest.raises(SlotTaken):
        svc.create("sess-2", plan2, writes2)
    assert store.get_booking(plan2.code) is None
    assert store.jobs(plan2.code) == []


def test_db_conflict_releases_in_memory_slot(
    svc: BookingService, store: SqliteStore, repo: InMemorySlotRepository
) -> None:
    # another process reserved the slot in the DB, but this process's picker did not know
    with store.transaction() as tx:
        tx.reserve_slot(SLOT.slot_id, "NL-X222")
    plan, writes = _writes(svc)
    with pytest.raises(SlotTaken):
        svc.create("sess-1", plan, writes)
    assert SLOT in repo.free_slots(SLOT.start_utc, OTHER.end_utc)  # released again
    assert store.get_booking(plan.code) is None


def test_missing_approved_call_rejected(svc: BookingService) -> None:
    plan, writes = _writes(svc)
    writes.pop(GMAIL_DRAFT)
    with pytest.raises(ValueError):
        svc.create("sess-1", plan, writes)


def test_store_persists_across_connections(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path}/sub/agent.db"
    repo = InMemorySlotRepository([SLOT])
    plan, writes = _writes(BookingService(SqliteStore(url), repo, fixed_clock))
    BookingService(SqliteStore(url), repo, fixed_clock).create("s", plan, writes)
    again = SqliteStore(url)
    assert again.get_booking(plan.code) is not None
    assert again.reserved_slot_ids() == [SLOT.slot_id]


def test_sqlite_path() -> None:
    assert sqlite_path("sqlite:///./data/agent.db") == "./data/agent.db"
    assert sqlite_path("sqlite:///:memory:") == ":memory:"
    with pytest.raises(ValueError):
        sqlite_path("postgresql://x")


def test_tokens_single_use(store: SqliteStore) -> None:
    store.insert_token("j1", "NL-A742", 10.0)
    assert store.mark_token_used("j1", 5.0) is True
    assert store.mark_token_used("j1", 6.0) is False
    assert store.get_token("j1").used_at == 5.0  # type: ignore[union-attr]
