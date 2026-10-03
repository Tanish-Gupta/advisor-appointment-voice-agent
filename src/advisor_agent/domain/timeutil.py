"""Time utilities and the single user-facing slot format (LLD 1.2).

Everything is stored in UTC; everything shown to the user is IST with the literal suffix "IST".
"""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from advisor_agent.domain.models import Preference, Slot

IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ist() -> datetime:
    return datetime.now(IST)


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")


def to_utc(dt: datetime) -> datetime:
    _require_aware(dt)
    return dt.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    _require_aware(dt)
    return dt.astimezone(IST)


def ist_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def fmt_time(t: time) -> str:
    """'2:00 PM' (12-hour clock, no leading zero)."""
    hour12 = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{hour12}:{t.minute:02d} {suffix}"


def fmt_date(d: date) -> str:
    """'Tuesday, 6 October 2026'."""
    return f"{d:%A}, {d.day} {d:%B} {d.year}"


def fmt_slot(slot: Slot) -> str:
    """'Tuesday, 6 October 2026, 2:00 PM IST' - the ONLY format used for offers and confirmations.

    This is the plan's formatSlotForUser(slot). Phase 7 derives the spoken form from this string.
    """
    start = to_ist(slot.start_utc)
    return f"{fmt_date(start.date())}, {fmt_time(start.time())} IST"


def fmt_preference(pref: Preference) -> str:
    """Human label for a requested day/window, e.g. 'Tuesday, 6 October 2026 (afternoon)'."""
    day = fmt_date(pref.date) if pref.date else "any working day"
    return f"{day} ({pref.window.label})" if pref.window else day
