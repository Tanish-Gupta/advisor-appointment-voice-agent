"""Shared fixtures: a frozen clock (Friday 2 Oct 2026, 16:00 IST) and a deterministic calendar."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pytest

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import ChatService
from advisor_agent.config import Settings
from advisor_agent.domain.models import Slot
from advisor_agent.domain.slots import generate_free_slots
from advisor_agent.domain.timeutil import IST, to_utc
from advisor_agent.orchestrator.audit import InMemoryAuditSink

NOW = to_utc(datetime(2026, 10, 2, 16, 0, tzinfo=IST))


def fixed_clock() -> datetime:
    return NOW


def default_slots() -> list[Slot]:
    return generate_free_slots(date(2026, 10, 2), working_days=14, seed=42)


def make_settings(**overrides: Any) -> Settings:
    # gemini_api_key=None: a real key in the environment must never reach the test suite.
    # mcp_tools_enabled=False: a developer .env with Google enabled must not hit real APIs.
    base: dict[str, Any] = {
        "env": "test", "nlu_engine": "stub", "record_golden": False, "gemini_api_key": None,
        "mcp_tools_enabled": False, "database_url": "sqlite:///:memory:",
    }  # fmt: skip
    return Settings(**{**base, **overrides})


@pytest.fixture
def audit() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def make_service(audit: InMemoryAuditSink) -> Callable[..., ChatService]:
    def _make(slots: list[Slot] | None = None, **settings: Any) -> ChatService:
        return build_chat_service(
            make_settings(**settings),
            slots=default_slots() if slots is None else slots,
            clock=fixed_clock,
            audit=audit,
        )

    return _make


@pytest.fixture
def service(make_service: Callable[..., ChatService]) -> ChatService:
    return make_service()
