"""Outbox worker (LLD 4.2): executes queued MCP write calls with retries and compensation.

Jobs were approved by the ToolGate when the user confirmed; the worker re-checks them
(`check_job`, defence in depth), calls the real FastMCP Google tools through the runner and
records the result. Retryable failures back off (5s, 15s, 60s, 5m, 15m); after that the job is
dead and compensation runs:

* dead `calendar_create_hold` -> booking `needs_attention`, notes line `hold_failed`,
  advisor `ops_alert` draft;
* dead `calendar_create_hold` of a reschedule -> additionally the pending delete of the old
  hold is skipped, so the client never ends up with no hold at all (LLD 6.1);
* dead `calendar_delete_hold` -> advisor `ops_alert` draft;
* dead notes/email jobs are logged (visible at /admin/outbox).
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from advisor_agent.domain.booking_service import NewJob
from advisor_agent.domain.models import BookingStatus
from advisor_agent.domain.plan import CREATE_HOLD, DELETE_HOLD, DOCS_APPEND, GMAIL_DRAFT
from advisor_agent.domain.timeutil import fmt_slot, to_ist
from advisor_agent.mcp_client.client import ToolCallError
from advisor_agent.mcp_client.runner import GatedToolRunner
from advisor_agent.nlu.tool_agent import ProposedCall
from advisor_agent.storage.sqlite import JobRow, SqliteStore

log = logging.getLogger(__name__)

BACKOFF_S: tuple[int, ...] = (5, 15, 60, 300, 900)


def compensation_key(code: str, tool: str, tag: int | str = 1) -> str:
    return f"{code}:compensate:{tool}:{tag}"


def _tag(job: JobRow) -> str:
    """'NL-A742:calendar_create_hold:2' -> '2' (the booking version, or 'cancel')."""
    return job.idempotency_key.rsplit(":", 1)[-1]


class OutboxWorker:
    def __init__(
        self,
        store: SqliteStore,
        runner: GatedToolRunner,
        clock: Callable[[], datetime],
        *,
        backoff_s: tuple[int, ...] = BACKOFF_S,
        batch: int = 10,
    ) -> None:
        self._store = store
        self._runner = runner
        self._clock = clock
        self._backoff = backoff_s
        self._batch = batch
        self._wake = asyncio.Event()
        self._stopping = False

    # --- scheduling ----------------------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run_once(self) -> int:
        """Process every job that is due now. Returns the number of jobs processed."""
        jobs = self._store.claim_due_jobs(self._clock(), self._batch)
        for job in jobs:
            await self._process(job)
        return len(jobs)

    async def drain(self, max_rounds: int = 50) -> int:
        """Run until no job is due (used by tests and the CLI). Returns jobs processed."""
        total = 0
        for _ in range(max_rounds):
            n = await self.run_once()
            if n == 0:
                break
            total += n
        return total

    async def run_forever(self, poll_s: float = 1.0) -> None:
        while not self._stopping:
            try:
                await self.drain()
            except Exception:  # never let the worker die; jobs stay in the table
                log.exception("outbox worker iteration failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=poll_s)

    # --- one job -------------------------------------------------------------------------

    async def _process(self, job: JobRow) -> None:
        call = ProposedCall(job.tool, dict(job.args))
        decision = self._runner.gate.check_job(call)
        if not decision.approved:
            self._dead(job, f"gate:{decision.rule}")
            return
        try:
            result = await self._runner.call(call)
        except ToolCallError as e:
            self._failed(job, str(e), retryable=e.retryable)
            return
        except Exception as e:  # unexpected client bug: treat as transient
            self._failed(job, f"{type(e).__name__}: {e}", retryable=True)
            return
        now = self._clock()
        self._store.mark_done(job.id, result, now)
        if job.tool == CREATE_HOLD and result.get("event_id"):
            self._store.set_calendar_event_id(job.booking_code, str(result["event_id"]), now)
        log.info("outbox job done", extra={"job_key": job.idempotency_key, "tool": job.tool})

    def _failed(self, job: JobRow, error: str, *, retryable: bool) -> None:
        if retryable and job.attempts < len(self._backoff):
            now = self._clock()
            next_run = now + timedelta(seconds=self._backoff[job.attempts])
            self._store.mark_retry(job.id, next_run, error, now)
            log.warning(
                "outbox job retry",
                extra={"job_key": job.idempotency_key, "attempt": job.attempts + 1},
            )
            return
        self._dead(job, error)

    def _dead(self, job: JobRow, error: str) -> None:
        self._store.mark_dead(job.id, error, self._clock())
        log.error("outbox job dead", extra={"job_key": job.idempotency_key, "error": error[:200]})
        if ":compensate:" in job.idempotency_key:
            return  # never compensate a compensation
        if job.tool == CREATE_HOLD:
            skipped = self._store.skip_pending(
                job.booking_code, DELETE_HOLD, _tag(job), "skipped: new hold not created",
                self._clock(),
            )  # fmt: skip
            if skipped:
                log.warning("old hold kept", extra={"job_key": job.idempotency_key})
        if job.tool in (CREATE_HOLD, DELETE_HOLD):
            self._compensate(job)

    def _compensate(self, job: JobRow) -> None:
        now = self._clock()
        code = job.booking_code
        booking = self._store.get_booking(code)
        topic = booking.topic.value if booking else str(job.args.get("topic", "unknown"))
        slot_label = fmt_slot(booking.slot) if booking and booking.slot else None
        tag = _tag(job)
        jobs: list[NewJob] = []
        if job.tool == CREATE_HOLD:
            self._store.set_booking_status(code, BookingStatus.NEEDS_ATTENTION, now)
            docs_args: dict[str, Any] = {
                "date": to_ist(now).date().isoformat(),
                "topic": topic,
                "slot": slot_label or "n/a",
                "code": code,
                "status": "hold_failed",
            }
            jobs.append(
                NewJob(compensation_key(code, DOCS_APPEND, tag), code, DOCS_APPEND, docs_args)
            )
        gmail_args = {"template": "ops_alert", "code": code, "topic": topic, "slot": slot_label}
        jobs.append(NewJob(compensation_key(code, GMAIL_DRAFT, tag), code, GMAIL_DRAFT, gmail_args))
        for new in jobs:
            self._store.enqueue(new, now)
        self.wake()
