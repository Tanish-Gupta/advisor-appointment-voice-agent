"""Session storage (LLD 2.2). In-memory with TTL; Redis arrives with Phase 3 infra."""

import threading
from datetime import datetime, timedelta
from typing import Protocol

from advisor_agent.domain.slots import Clock
from advisor_agent.orchestrator.context import SessionContext


class SessionStore(Protocol):
    def load(self, session_id: str) -> SessionContext | None: ...
    def save(self, ctx: SessionContext) -> None: ...
    def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    def __init__(self, ttl: timedelta, clock: Clock) -> None:
        self._ttl = ttl
        self._clock = clock
        self._data: dict[str, tuple[SessionContext, datetime]] = {}
        self._lock = threading.Lock()

    def load(self, session_id: str) -> SessionContext | None:
        with self._lock:
            item = self._data.get(session_id)
            if item is None:
                return None
            ctx, expires = item
            if self._clock() >= expires:
                del self._data[session_id]
                return None
            return ctx.model_copy(deep=True)

    def save(self, ctx: SessionContext) -> None:
        with self._lock:
            self._data[ctx.session_id] = (ctx.model_copy(deep=True), self._clock() + self._ttl)

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            expired = [k for k, (_, exp) in self._data.items() if now >= exp]
            for k in expired:
                del self._data[k]
        return len(expired)
