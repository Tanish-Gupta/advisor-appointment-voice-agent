"""SQLite persistence for Phase 4 (LLD 0.4): bookings, slot reservations, outbox jobs,
secure-link tokens and the PII vault.

Uses the standard-library `sqlite3` (no ORM dependency). One connection is shared and
serialised with a lock; transactions use `BEGIN IMMEDIATE`. Timestamps that are compared
(`next_run_at`, `expires_at`, `used_at`) are stored as UNIX seconds; display timestamps as ISO.
Phase 8 swaps this for Postgres behind the same methods.
"""

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from advisor_agent.domain.booking_service import NewJob
from advisor_agent.domain.models import Booking, BookingKind, BookingStatus, Slot, Topic
from advisor_agent.domain.slots import SlotTaken

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  code               TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  topic              TEXT NOT NULL,
  slot_id            TEXT,
  slot_start_utc     TEXT,
  slot_end_utc       TEXT,
  status             TEXT NOT NULL,
  calendar_event_id  TEXT,
  version            INTEGER NOT NULL DEFAULT 1,
  pref_label         TEXT,
  session_id         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_session_once
  ON bookings(session_id, kind) WHERE status IN ('tentative', 'waitlist');

CREATE TABLE IF NOT EXISTS slot_reservations (
  slot_id       TEXT PRIMARY KEY,
  booking_code  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_jobs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key  TEXT UNIQUE NOT NULL,
  booking_code     TEXT NOT NULL,
  tool             TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending',
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_run_at      REAL NOT NULL,
  result_json      TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox_jobs(status, next_run_at);

CREATE TABLE IF NOT EXISTS secure_tokens (
  jti           TEXT PRIMARY KEY,
  booking_code  TEXT NOT NULL,
  expires_at    REAL NOT NULL,
  used_at       REAL
);

CREATE TABLE IF NOT EXISTS pii_vault (
  booking_code  TEXT PRIMARY KEY,
  ciphertext    BLOB NOT NULL,
  created_at    TEXT NOT NULL
);
"""

# Columns added after Phase 4 (ALTER TABLE for databases created by an older build).
_MIGRATIONS = {
    "version": "ALTER TABLE bookings ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    "pref_label": "ALTER TABLE bookings ADD COLUMN pref_label TEXT",
}

_ACTIVE = (BookingStatus.TENTATIVE.value, BookingStatus.WAITLIST.value)


def sqlite_path(database_url: str) -> str:
    """'sqlite:///./agent.db' -> './agent.db'; ':memory:' and plain paths pass through."""
    if database_url in (":memory:", "sqlite://", "sqlite:///:memory:"):
        return ":memory:"
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    if "://" in database_url:
        raise ValueError(f"only sqlite URLs are supported in Phase 4: {database_url!r}")
    return database_url


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class JobRow:
    id: int
    idempotency_key: str
    booking_code: str
    tool: str
    args: dict[str, Any]
    status: str  # pending | running | done | dead
    attempts: int
    next_run_at: float
    result: dict[str, Any] | None
    last_error: str | None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "idempotency_key": self.idempotency_key,
            "booking_code": self.booking_code,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "attempts": self.attempts,
            "next_run_at": self.next_run_at,
            "result": self.result,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class TokenRow:
    jti: str
    booking_code: str
    expires_at: float
    used_at: float | None


class _Tx:
    """BookingTx implementation bound to an open transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def code_exists(self, code: str) -> bool:
        return (
            self._c.execute("SELECT 1 FROM bookings WHERE code=?", (code,)).fetchone() is not None
        )

    def reserve_slot(self, slot_id: str, code: str) -> None:
        try:
            self._c.execute(
                "INSERT INTO slot_reservations(slot_id, booking_code) VALUES (?, ?)",
                (slot_id, code),
            )
        except sqlite3.IntegrityError:
            raise SlotTaken(slot_id) from None

    def release_slot(self, slot_id: str) -> None:
        self._c.execute("DELETE FROM slot_reservations WHERE slot_id=?", (slot_id,))

    def insert_booking(self, booking: Booking, session_id: str, now: datetime) -> None:
        slot = booking.slot
        self._c.execute(
            "INSERT INTO bookings(code, kind, topic, slot_id, slot_start_utc, slot_end_utc, status,"
            " calendar_event_id, version, pref_label, session_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                booking.code,
                booking.kind.value,
                booking.topic.value,
                slot.slot_id if slot else None,
                _iso(slot.start_utc) if slot else None,
                _iso(slot.end_utc) if slot else None,
                booking.status.value,
                booking.calendar_event_id,
                booking.version,
                booking.pref_label,
                session_id,
                _iso(now),
                _iso(now),
            ),
        )

    def enqueue(self, job: NewJob, now: datetime) -> None:
        # INSERT OR IGNORE: the idempotency key is unique, re-enqueueing is a no-op.
        self._c.execute(
            "INSERT OR IGNORE INTO outbox_jobs(idempotency_key, booking_code, tool, payload_json,"
            " status, attempts, next_run_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
            (
                job.idempotency_key,
                job.booking_code,
                job.tool,
                json.dumps(job.args, sort_keys=True),
                now.timestamp(),
                _iso(now),
                _iso(now),
            ),
        )

    def set_booking_status(self, code: str, status: BookingStatus, now: datetime) -> None:
        self._c.execute(
            "UPDATE bookings SET status=?, updated_at=? WHERE code=?",
            (status.value, _iso(now), code),
        )

    def move_booking(self, code: str, slot: Slot, version: int, now: datetime) -> None:
        """Reschedule: new slot + version; the booking is tentative again (LLD 6.1)."""
        self._c.execute(
            "UPDATE bookings SET slot_id=?, slot_start_utc=?, slot_end_utc=?, version=?,"
            " status=?, calendar_event_id=NULL, updated_at=? WHERE code=?",
            (
                slot.slot_id,
                _iso(slot.start_utc),
                _iso(slot.end_utc),
                version,
                BookingStatus.TENTATIVE.value,
                _iso(now),
                code,
            ),
        )


class SqliteStore:
    """Implements domain.booking_service.BookingStore plus outbox, token and vault storage."""

    def __init__(self, database_url: str = ":memory:") -> None:
        path = sqlite_path(database_url)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(bookings)")}
            for col, ddl in _MIGRATIONS.items():
                if col not in cols:
                    self._conn.execute(ddl)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- transactions --------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[_Tx]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield _Tx(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    # --- bookings ------------------------------------------------------------------------

    @staticmethod
    def _booking(row: sqlite3.Row) -> Booking:
        slot = None
        if row["slot_id"]:
            slot = Slot(
                slot_id=row["slot_id"],
                start_utc=datetime.fromisoformat(row["slot_start_utc"]),
                end_utc=datetime.fromisoformat(row["slot_end_utc"]),
            )
        return Booking(
            code=row["code"],
            kind=BookingKind(row["kind"]),
            topic=Topic(row["topic"]),
            slot=slot,
            preference=None,
            status=BookingStatus(row["status"]),
            calendar_event_id=row["calendar_event_id"],
            version=row["version"],
            pref_label=row["pref_label"],
        )

    def code_exists(self, code: str) -> bool:
        return self._one("SELECT 1 FROM bookings WHERE code=?", (code,)) is not None

    def get_booking(self, code: str) -> Booking | None:
        row = self._one("SELECT * FROM bookings WHERE code=?", (code,))
        return self._booking(row) if row else None

    def active_booking_for_session(self, session_id: str) -> Booking | None:
        row = self._one(
            "SELECT * FROM bookings WHERE session_id=? AND status IN (?, ?)"
            " ORDER BY created_at DESC LIMIT 1",
            (session_id, *_ACTIVE),
        )
        return self._booking(row) if row else None

    def set_calendar_event_id(self, code: str, event_id: str, now: datetime) -> None:
        self._exec(
            "UPDATE bookings SET calendar_event_id=?, updated_at=? WHERE code=?",
            (event_id, _iso(now), code),
        )

    def set_booking_status(self, code: str, status: BookingStatus, now: datetime) -> None:
        self._exec(
            "UPDATE bookings SET status=?, updated_at=? WHERE code=?",
            (status.value, _iso(now), code),
        )

    def reserved_slot_ids(self) -> list[str]:
        return [r["slot_id"] for r in self._all("SELECT slot_id FROM slot_reservations")]

    # --- outbox --------------------------------------------------------------------------

    @staticmethod
    def _job(row: sqlite3.Row) -> JobRow:
        return JobRow(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            booking_code=row["booking_code"],
            tool=row["tool"],
            args=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            next_run_at=row["next_run_at"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            last_error=row["last_error"],
        )

    def enqueue(self, job: NewJob, now: datetime) -> None:
        with self.transaction() as tx:
            tx.enqueue(job, now)

    def claim_due_jobs(self, now: datetime, limit: int = 10) -> list[JobRow]:
        """Atomically move due jobs to 'running'. A job is due only when every earlier job of
        the same booking is finished (done/dead), so jobs of one booking run in insertion order."""
        ts = now.timestamp()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM outbox_jobs j WHERE j.status='pending' AND j.next_run_at<=?"
                    " AND NOT EXISTS (SELECT 1 FROM outbox_jobs p"
                    " WHERE p.booking_code=j.booking_code"
                    " AND p.id<j.id AND p.status IN ('pending', 'running'))"
                    " ORDER BY j.id LIMIT ?",
                    (ts, limit),
                ).fetchall()
                for r in rows:
                    self._conn.execute(
                        "UPDATE outbox_jobs SET status='running', updated_at=? WHERE id=?",
                        (_iso(now), r["id"]),
                    )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        return [self._job(r) for r in rows]

    def mark_done(self, job_id: int, result: dict[str, Any], now: datetime) -> None:
        self._exec(
            "UPDATE outbox_jobs SET status='done', attempts=attempts+1, result_json=?,"
            " last_error=NULL, updated_at=? WHERE id=?",
            (json.dumps(result, sort_keys=True, default=str), _iso(now), job_id),
        )

    def mark_retry(self, job_id: int, next_run_at: datetime, error: str, now: datetime) -> None:
        self._exec(
            "UPDATE outbox_jobs SET status='pending', attempts=attempts+1, next_run_at=?,"
            " last_error=?, updated_at=? WHERE id=?",
            (next_run_at.timestamp(), error[:500], _iso(now), job_id),
        )

    def mark_dead(self, job_id: int, error: str, now: datetime) -> None:
        self._exec(
            "UPDATE outbox_jobs SET status='dead', attempts=attempts+1, last_error=?, updated_at=?"
            " WHERE id=?",
            (error[:500], _iso(now), job_id),
        )

    def skip_pending(self, booking_code: str, tool: str, key_suffix: str, reason: str,
                     now: datetime) -> int:  # fmt: skip
        """Mark pending jobs of a booking dead without running them (e.g. the old-hold delete
        of a reschedule whose new hold could not be created). Returns the number skipped."""
        return self._exec(
            "UPDATE outbox_jobs SET status='dead', last_error=?, updated_at=?"
            " WHERE booking_code=? AND tool=? AND status='pending' AND idempotency_key LIKE ?",
            (reason[:500], _iso(now), booking_code, tool, f"%:{key_suffix}"),
        )

    def requeue_running(self) -> int:
        """At startup: jobs left 'running' by a crash become 'pending' again."""
        return self._exec("UPDATE outbox_jobs SET status='pending' WHERE status='running'")

    def get_job(self, job_id: int) -> JobRow | None:
        row = self._one("SELECT * FROM outbox_jobs WHERE id=?", (job_id,))
        return self._job(row) if row else None

    def jobs(self, booking_code: str | None = None) -> list[JobRow]:
        if booking_code:
            rows = self._all(
                "SELECT * FROM outbox_jobs WHERE booking_code=? ORDER BY id", (booking_code,)
            )
        else:
            rows = self._all("SELECT * FROM outbox_jobs ORDER BY id DESC LIMIT 200")
        return [self._job(r) for r in rows]

    def job_by_key(self, key: str) -> JobRow | None:
        row = self._one("SELECT * FROM outbox_jobs WHERE idempotency_key=?", (key,))
        return self._job(row) if row else None

    # --- secure tokens -------------------------------------------------------------------

    def insert_token(self, jti: str, booking_code: str, expires_at: float) -> None:
        self._exec(
            "INSERT INTO secure_tokens(jti, booking_code, expires_at) VALUES (?, ?, ?)",
            (jti, booking_code, expires_at),
        )

    def get_token(self, jti: str) -> TokenRow | None:
        row = self._one("SELECT * FROM secure_tokens WHERE jti=?", (jti,))
        if row is None:
            return None
        return TokenRow(row["jti"], row["booking_code"], row["expires_at"], row["used_at"])

    def mark_token_used(self, jti: str, used_at: float) -> bool:
        """Atomic single use: True only for the first caller."""
        n = self._exec(
            "UPDATE secure_tokens SET used_at=? WHERE jti=? AND used_at IS NULL", (used_at, jti)
        )
        return n == 1

    # --- PII vault -----------------------------------------------------------------------

    def put_vault(self, booking_code: str, ciphertext: bytes, now: datetime) -> None:
        self._exec(
            "INSERT INTO pii_vault(booking_code, ciphertext, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(booking_code) DO UPDATE SET ciphertext=excluded.ciphertext,"
            " created_at=excluded.created_at",
            (booking_code, ciphertext, _iso(now)),
        )

    def get_vault(self, booking_code: str) -> bytes | None:
        row = self._one("SELECT ciphertext FROM pii_vault WHERE booking_code=?", (booking_code,))
        return bytes(row["ciphertext"]) if row else None
