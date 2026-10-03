"""Audit trail. Only redacted text is ever recorded. Phase 4 adds the DB-backed sink."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger("advisor_agent.audit")


@dataclass(frozen=True)
class AuditEvent:
    at: datetime
    session_id: str
    kind: str  # turn | transition | error
    data: dict[str, Any] = field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
        log.info(event.kind, extra={"session_id": event.session_id, **event.data})
