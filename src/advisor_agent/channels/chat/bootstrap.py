"""Composition root for the chat channel: wires settings -> calendar -> orchestrator -> service.

With AGENT_MCP_TOOLS_ENABLED=true it also builds the Phase 3B/4 runtime: SQLite store, FastMCP
ToolClient, ToolGate, Gemini tool agent, outbox worker, secure links and PII vault.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from advisor_agent.channels.chat.service import ChatService
from advisor_agent.config import Settings
from advisor_agent.domain.booking_service import BookingService
from advisor_agent.domain.models import Slot
from advisor_agent.domain.slots import (
    Clock,
    InMemorySlotRepository,
    SlotPicker,
    SlotTaken,
    generate_free_slots,
    slots_from_mock_calendar,
)
from advisor_agent.domain.timeutil import now_ist, now_utc
from advisor_agent.mcp_client.client import ToolClient
from advisor_agent.mcp_client.executor import McpBookingExecutor
from advisor_agent.mcp_client.outbox import OutboxWorker
from advisor_agent.mcp_client.runner import GatedToolRunner
from advisor_agent.mcp_client.tool_gate import ToolGate
from advisor_agent.nlu.engine import NluEngine, build_engine
from advisor_agent.nlu.tool_agent import GeminiToolAgent, ToolGenerateFn, google_tool_generate
from advisor_agent.orchestrator.audit import AuditSink, InMemoryAuditSink
from advisor_agent.orchestrator.fsm import Orchestrator
from advisor_agent.orchestrator.handlers.base import Deps
from advisor_agent.orchestrator.session_store import InMemorySessionStore
from advisor_agent.secure.tokens import SecureLinks, resolve_secret
from advisor_agent.secure.vault import PiiVault, resolve_key
from advisor_agent.storage.sqlite import SqliteStore

log = logging.getLogger(__name__)


def load_slots(settings: Settings, clock: Clock) -> list[Slot]:
    """Mock calendar JSON; if it has no future slots (stale file), generate fresh ones."""
    path = Path(settings.mock_calendar_path)
    slots: list[Slot] = []
    if path.exists():
        slots = slots_from_mock_calendar(json.loads(path.read_text()))
    if not any(s.start_utc > clock() for s in slots):
        slots = generate_free_slots(now_ist().date(), working_days=settings.booking_horizon_days)
    return slots


def build_chat_service(
    settings: Settings,
    *,
    slots: list[Slot] | None = None,
    clock: Clock = now_utc,
    nlu: NluEngine | None = None,
    audit: AuditSink | None = None,
    scenario: str | None = None,
    tool_target: Any = None,
    tool_generate: ToolGenerateFn | None = None,
) -> ChatService:
    """`tool_target` overrides AGENT_MCP_SERVER_TARGET (tests pass a FastMCP server object) and
    enables the MCP runtime; `tool_generate` overrides the Gemini tool-calling function."""
    repo = InMemorySlotRepository(slots if slots is not None else load_slots(settings, clock))
    picker = SlotPicker(
        repo,
        clock,
        horizon_days=settings.booking_horizon_days,
        lead_time=timedelta(minutes=settings.lead_time_min),
        business_hours=settings.business_hours,
    )
    runtime = None
    if settings.mcp_tools_enabled or tool_target is not None:
        runtime = build_runtime(
            settings, repo, clock, tool_target=tool_target, tool_generate=tool_generate
        )
    store = InMemorySessionStore(timedelta(minutes=settings.session_ttl_min), clock)
    orchestrator = Orchestrator(
        store=store,
        nlu=nlu or build_engine(settings),
        deps=Deps(
            picker=picker,
            clock=clock,
            horizon_days=settings.booking_horizon_days,
            stub_hints=settings.nlu_engine == "stub",
            confidence_threshold=settings.nlu_confidence_threshold,
            business_hours=settings.business_hours,
            executor=runtime.executor if runtime else None,
        ),
        audit=audit or InMemoryAuditSink(),
        strict=settings.env != "prod",
    )
    golden_dir = Path(settings.golden_dir) if settings.record_golden else None
    return ChatService(
        orchestrator, store, golden_dir=golden_dir, scenario=scenario, runtime=runtime
    )


@dataclass
class Runtime:
    """Phase 3B/4 objects shared by the chat API, the CLI and the outbox worker."""

    store: SqliteStore
    tool_client: ToolClient
    runner: GatedToolRunner
    worker: OutboxWorker
    links: SecureLinks
    vault: PiiVault
    bookings: BookingService
    executor: McpBookingExecutor
    poll_s: float = 1.0
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def start(self, *, run_worker: bool = True) -> None:
        requeued = self.store.requeue_running()
        if requeued:
            log.info("requeued %d interrupted outbox jobs", requeued)
        await self.tool_client.open()
        await self.tool_client.declarations()  # fail fast if the tool surface is wrong
        if run_worker and self._task is None:
            self._task = asyncio.create_task(self.worker.run_forever(self.poll_s))

    async def stop(self, *, drain: bool = False) -> None:
        self.worker.stop()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)
            self._task = None
        if drain:
            with contextlib.suppress(Exception):
                await self.worker.drain()
        await self.tool_client.aclose()


def build_runtime(
    settings: Settings,
    repo: InMemorySlotRepository,
    clock: Clock,
    *,
    tool_target: Any = None,
    tool_generate: ToolGenerateFn | None = None,
) -> Runtime:
    store = SqliteStore(settings.database_url)
    for slot_id in store.reserved_slot_ids():  # booked slots stay booked across restarts
        with contextlib.suppress(SlotTaken):
            repo.reserve(slot_id, "persisted")

    client = (
        ToolClient(tool_target, call_timeout_s=settings.mcp_call_timeout_s)
        if tool_target is not None
        else ToolClient.from_settings(settings)
    )
    gate = ToolGate(
        clock,
        horizon_days=settings.booking_horizon_days,
        business_hours=settings.business_hours,
        max_calls_per_step=settings.mcp_max_tool_calls_per_turn,
    )
    agent = None
    if settings.mcp_llm_tool_calling:
        generate = tool_generate
        if generate is None and settings.gemini_api_key is not None:
            try:
                generate = google_tool_generate(
                    settings.gemini_api_key.get_secret_value(), settings.gemini_model
                )
            except ImportError:
                log.warning("google-genai not installed; MCP calls use the deterministic plan")
        if generate is not None:
            agent = GeminiToolAgent(
                generate, client.declarations, timeout_s=settings.mcp_agent_timeout_s
            )
    runner = GatedToolRunner(client, gate, agent, clock)
    worker = OutboxWorker(store, runner, clock)
    links = SecureLinks(
        resolve_secret(
            settings.link_hmac_secret.get_secret_value() if settings.link_hmac_secret else None,
            env=settings.env,
        ),
        settings.public_base_url,
        store,
        clock,
        ttl_hours=settings.link_ttl_hours,
    )
    vault = PiiVault(
        resolve_key(
            settings.vault_fernet_key.get_secret_value() if settings.vault_fernet_key else None,
            env=settings.env,
        ),
        store,
        clock,
    )
    bookings = BookingService(
        store,
        repo,
        clock,
        slot_minutes=settings.slot_duration_min,
        business_hours=settings.business_hours,
        waitlist_hold=settings.waitlist_mode == "hold",
        cancel_draft=settings.cancel_draft_enabled,
    )
    executor = McpBookingExecutor(runner, bookings, links, on_enqueued=worker.wake)
    return Runtime(
        store=store,
        tool_client=client,
        runner=runner,
        worker=worker,
        links=links,
        vault=vault,
        bookings=bookings,
        executor=executor,
        poll_s=settings.outbox_poll_s,
    )
