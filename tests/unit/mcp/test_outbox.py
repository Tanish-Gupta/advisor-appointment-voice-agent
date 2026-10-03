"""OutboxWorker: ordered execution, retries with backoff, dead letters and compensation."""

import json
from datetime import datetime, timedelta

import pytest

from advisor_agent.domain.booking_service import BookingService
from advisor_agent.domain.models import BookingStatus, Topic
from advisor_agent.domain.plan import BOOKING_WRITES, CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT
from advisor_agent.domain.slots import InMemorySlotRepository, make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.mcp_client.client import ToolCallError
from advisor_agent.mcp_client.outbox import BACKOFF_S, OutboxWorker, compensation_key
from advisor_agent.mcp_client.runner import GatedToolRunner
from advisor_agent.mcp_client.tool_gate import ToolGate
from advisor_agent.storage.sqlite import SqliteStore
from tests.conftest import NOW

PHONE = "+919876543210"
SLOT = make_slot(datetime(2026, 10, 6, 13, 0, tzinfo=IST))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ScriptedCaller:
    """Fails `tool` with the given errors in order, then succeeds."""

    def __init__(self) -> None:
        self.errors: dict[str, list[ToolCallError]] = {}
        self.calls: list[str] = []

    async def call(self, tool: str, args: dict) -> dict:
        self.calls.append(tool)
        if self.errors.get(tool):
            raise self.errors[tool].pop(0)
        return {"event_id": "evt123"} if tool == CREATE_HOLD else {"ok": True}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def env(clock: Clock):  # type: ignore[no-untyped-def]
    store = SqliteStore(":memory:")
    caller = ScriptedCaller()
    runner = GatedToolRunner(caller, ToolGate(clock), None, clock)
    worker = OutboxWorker(store, runner, clock)
    svc = BookingService(store, InMemorySlotRepository([SLOT]), clock)
    plan = svc.plan(Topic.SIP_MANDATES, SLOT)
    calls = plan.expected_calls()
    svc.create("sess", plan, {t: calls[t] for t in BOOKING_WRITES})
    return store, caller, worker, plan.code


def _status(store: SqliteStore, code: str) -> dict[str, str]:
    return {j.idempotency_key.split(":", 1)[1]: j.status for j in store.jobs(code)}


def _err(tool: str, retryable: bool) -> ToolCallError:
    return ToolCallError(
        tool, "retryable:503" if retryable else "permanent:403", retryable=retryable
    )


async def test_happy_path_runs_in_order_and_records_event_id(env) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    assert await worker.drain() == 3
    assert caller.calls == [CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT]
    assert set(_status(store, code).values()) == {"done"}
    assert store.get_booking(code).calendar_event_id == "evt123"
    assert await worker.drain() == 0  # nothing runs twice


async def test_retry_with_backoff_blocks_later_jobs(env, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    caller.errors[CREATE_HOLD] = [_err(CREATE_HOLD, True)]
    await worker.drain()
    assert caller.calls == [CREATE_HOLD]  # docs/gmail wait for the hold
    job = store.jobs(code)[0]
    assert job.status == "pending" and job.attempts == 1
    assert job.next_run_at == (clock.now + timedelta(seconds=BACKOFF_S[0])).timestamp()

    clock.advance(BACKOFF_S[0] - 1)
    assert await worker.drain() == 0
    clock.advance(1)
    await worker.drain()
    assert caller.calls == [CREATE_HOLD, CREATE_HOLD, DOCS_APPEND, GMAIL_DRAFT]
    assert store.get_booking(code).status is BookingStatus.TENTATIVE


async def test_dead_create_hold_compensates(env, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    caller.errors[CREATE_HOLD] = [_err(CREATE_HOLD, True) for _ in range(len(BACKOFF_S) + 1)]
    for delay in (*BACKOFF_S, 0):
        await worker.drain()
        clock.advance(delay)
    await worker.drain()

    hold = store.job_by_key(f"{code}:{CREATE_HOLD}:1")
    assert hold.status == "dead" and hold.attempts == len(BACKOFF_S) + 1
    assert store.get_booking(code).status is BookingStatus.NEEDS_ATTENTION
    docs = store.job_by_key(compensation_key(code, DOCS_APPEND))
    alert = store.job_by_key(compensation_key(code, GMAIL_DRAFT))
    assert docs.status == "done" and docs.args["status"] == "hold_failed"
    assert alert.status == "done" and alert.args["template"] == "ops_alert"
    assert "+91" not in str(alert.args) and alert.args["slot"]


async def test_permanent_error_is_dead_immediately(env) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    caller.errors[CREATE_HOLD] = [_err(CREATE_HOLD, False)]
    await worker.drain()
    assert store.job_by_key(f"{code}:{CREATE_HOLD}:1").attempts == 1
    assert store.job_by_key(compensation_key(code, GMAIL_DRAFT)) is not None


async def test_dead_docs_job_does_not_compensate(env) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    caller.errors[DOCS_APPEND] = [_err(DOCS_APPEND, False)]
    await worker.drain()
    assert _status(store, code) == {
        f"{CREATE_HOLD}:1": "done", f"{DOCS_APPEND}:1": "dead", f"{GMAIL_DRAFT}:1": "done",
    }  # fmt: skip
    assert store.get_booking(code).status is BookingStatus.TENTATIVE


async def test_failing_compensation_is_not_compensated(env) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    caller.errors[CREATE_HOLD] = [_err(CREATE_HOLD, False)]
    caller.errors[GMAIL_DRAFT] = [_err(GMAIL_DRAFT, False), _err(GMAIL_DRAFT, False)]
    await worker.drain()
    keys = [j.idempotency_key for j in store.jobs(code)]
    assert len(keys) == 5 and len(set(keys)) == 5


async def test_gate_blocks_tampered_job(env) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    store._exec(  # simulate a tampered row
        "UPDATE outbox_jobs SET payload_json=? WHERE idempotency_key=?",
        (
            json.dumps({"template": "booking", "code": code, "topic": "x", "slot": PHONE}),
            f"{code}:{GMAIL_DRAFT}:1",
        ),  # fmt: skip
    )
    await worker.drain()
    job = store.job_by_key(f"{code}:{GMAIL_DRAFT}:1")
    assert job.status == "dead" and job.last_error == "gate:pii"
    assert GMAIL_DRAFT not in caller.calls


async def test_requeue_running_after_crash(env, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    store, caller, worker, code = env
    claimed = store.claim_due_jobs(clock(), 1)
    assert claimed[0].status == "pending"  # row snapshot; DB row is now running
    assert store.jobs(code)[0].status == "running"
    assert store.requeue_running() == 1
    await worker.drain()
    assert set(_status(store, code).values()) == {"done"}


def test_compensation_key() -> None:
    assert compensation_key("NL-A742", GMAIL_DRAFT) == "NL-A742:compensate:gmail_create_draft:1"


async def test_dead_reschedule_hold_skips_deleting_the_old_one(clock: Clock) -> None:
    from advisor_agent.domain.plan import DELETE_HOLD

    store = SqliteStore(":memory:")
    caller = ScriptedCaller()
    runner = GatedToolRunner(caller, ToolGate(clock), None, clock)
    worker = OutboxWorker(store, runner, clock)
    other = make_slot(datetime(2026, 10, 7, 10, 0, tzinfo=IST))
    svc = BookingService(store, InMemorySlotRepository([SLOT, other]), clock)
    plan = svc.plan(Topic.SIP_MANDATES, SLOT)
    calls = plan.expected_calls()
    svc.create("sess", plan, {t: calls[t] for t in BOOKING_WRITES})
    await worker.drain()

    move = svc.plan_reschedule(store.get_booking(plan.code), other)
    move_calls = move.expected_calls()
    svc.reschedule(move, {t: move_calls[t] for t in move.writes})
    caller.errors[CREATE_HOLD] = [_err(CREATE_HOLD, False)]  # permanent failure
    await worker.drain()

    code = plan.code
    assert store.job_by_key(f"{code}:{CREATE_HOLD}:2").status == "dead"
    delete = store.job_by_key(f"{code}:{DELETE_HOLD}:2")
    assert delete.status == "dead" and DELETE_HOLD not in caller.calls  # old hold is kept
    assert store.job_by_key(compensation_key(code, GMAIL_DRAFT, 2)).status == "done"
