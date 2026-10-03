"""RelativeDateResolver (LLD 3.4): deterministic day/time understanding.

Two jobs:
1. `extract_date` / `extract_time` find day and time phrases in free text (rules layer, 3.5)
   and return them as canonical strings ("next tuesday", "around 15:00", "afternoon").
2. `resolve_date` / `window_for` / `resolve` turn those strings into a domain `Preference`.

The resolver is the source of truth for dates: Gemini's `date_iso` is only a hint (3.6).
Pure and deterministic; `ref` (an aware "now") is always passed in.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from advisor_agent.domain.models import Preference, TimeWindow
from advisor_agent.domain.timeutil import fmt_time, ist_datetime, to_ist

__all__ = [
    "ANY",
    "WINDOWS",
    "PreferenceError",
    "RelativeDateResolver",
    "clock_times",
    "extract_date",
    "extract_time",
    "window_for",
]

ANY = "any"

WINDOWS: dict[str, TimeWindow] = {
    "morning": TimeWindow(start=time(9), end=time(12), label="morning"),
    "afternoon": TimeWindow(start=time(12), end=time(16), label="afternoon"),
    "evening": TimeWindow(start=time(16), end=time(18), label="evening"),
}


class PreferenceError(Exception):
    """reason: "out_of_range" | "non_working_day" | "outside_hours"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- vocabulary

WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5,
    "sunday": 6,
}  # fmt: skip
MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip
_SMALL_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                  "seven": 7, "ten": 10}  # fmt: skip

_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"

_ANY_RE = re.compile(
    r"\b(any ?time|any ?day|any slot|whenever|earliest|asap|as soon as possible|soonest"
    r"|first available|next available|no preference|no particular (?:day|time)"
    r"|doesn'?t matter|don'?t mind|(?:i'?m |am )?flexible|anything works)\b"
    r"|^(?:any|anything|either|any one|any is fine|any works)$"
)
_DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("day_after", re.compile(r"\bday after tomorrow\b")),
    ("relative", re.compile(r"\b(today|tonight|tomorrow|tmrw|tmr|tomorow)\b")),
    ("in_days", re.compile(r"\bin (\d{1,2}|a|one|two|three|four|five|six|seven|ten) days?\b")),
    ("next_week", re.compile(r"\b(?:next|coming) week\b")),
    ("weekday", re.compile(rf"\b(?:(this|next|coming|on)\s+)?({_WD})\b")),
    ("day_month", re.compile(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})\b")),
    ("month_day", re.compile(rf"\b({_MON})\s+(\d{{1,2}}){_ORD}\b")),
    ("slash", re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\b")),
    ("ordinal", re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b")),
]

_HM = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m|p\.m)?"
_NAMED_TIMES = {"noon": time(12), "midday": time(12), "lunch": time(13), "lunchtime": time(13),
                "lunch time": time(13)}  # fmt: skip
_NAMED = r"(noon|midday|lunch ?time|lunch)"
_TIME_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("between", re.compile(rf"\b(?:between|from) {_HM} ?(?:and|to|till|until|-) ?{_HM}\b")),
    ("after", re.compile(rf"\b(?:after|from|post|later than) (?:{_NAMED}|{_HM})\b")),
    ("before", re.compile(rf"\b(?:before|by|until|till|not later than) (?:{_NAMED}|{_HM})\b")),
    ("named", re.compile(rf"\b(?:around |about |at |by )?{_NAMED}\b")),
    ("clock", re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m|p\.m|o'?clock)\b")),
    ("colon", re.compile(r"\b(\d{1,2}):(\d{2})\b")),
    ("at", re.compile(
        r"\b(?:around|about|at|approximately|approx|say) (\d{1,2})\b"
        r"(?!\s*(?:days?|weeks?|st|nd|rd|th|/))"
    )),
    ("part", re.compile(r"\b(?:early |late )?(morning|forenoon|afternoon|evening|tonight)\b")),
]  # fmt: skip
_PART_ALIASES = {"forenoon": "morning", "tonight": "evening"}


# --------------------------------------------------------------------------- extraction


def _hour(h: int, minute: int, ampm: str | None) -> time | None:
    """12-hour/24-hour → time. Without am/pm, 1-8 mean PM (advisors work 9 AM-6 PM)."""
    if not 0 <= minute < 60:
        return None
    marker = (ampm or "").replace(".", "")
    if marker == "am":
        if not 1 <= h <= 12:
            return None
        h = 0 if h == 12 else h
    elif marker == "pm":
        if not 1 <= h <= 12:
            return None
        h = h if h == 12 else h + 12
    elif 1 <= h <= 8:
        h += 12
    if not 0 <= h <= 23:
        return None
    return time(h, minute)


def _hm(groups: tuple[str | None, ...], offset: int = 0) -> time | None:
    h, m, ampm = groups[offset : offset + 3]
    if h is None:
        return None
    return _hour(int(h), int(m or 0), ampm)


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s)


_WD_NEXT_WEEK = re.compile(rf"\b({_WD})\b.*\bnext week\b|\bnext week\b.*\b({_WD})\b")


def extract_date(text: str) -> str | None:
    """Return the canonical day phrase in normalised (lower-case) text, or None.

    'any' means "no day preference". Other values are understood by `resolve_date`.
    """
    if m := _WD_NEXT_WEEK.search(text):  # "tuesday next week" == "next tuesday"
        return f"next {m.group(1) or m.group(2)}"
    for kind, pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        if kind == "weekday":
            qualifier, wd = m.group(1), m.group(2)
            return f"next {wd}" if qualifier == "next" else wd
        if kind == "relative" and m.group(1) == "tonight":
            return "today"
        return m.group(0)
    if _ANY_RE.search(text):
        return ANY
    return None


def extract_time(text: str) -> str | None:
    """Return a canonical time phrase ('afternoon', 'around 15:00', 'after 16:00',
    'before 12:00', 'between 14:00 and 16:00') or None."""
    for kind, pattern in _TIME_PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        g = m.groups()
        if kind == "between":
            a, b = _hm(g, 0), _hm(g, 3)
            if a is not None and b is not None and a < b:
                return f"between {_hhmm(a)} and {_hhmm(b)}"
            continue
        if kind in ("after", "before"):
            t = _NAMED_TIMES.get(_squash(g[0])) if g[0] else _hm(g, 1)
            if t is not None:
                return f"{kind} {_hhmm(t)}"
            continue
        if kind == "named":
            return f"around {_hhmm(_NAMED_TIMES[_squash(g[0])])}"
        if kind == "clock":
            ampm = None if (g[2] or "").startswith("o") else g[2]
            t = _hour(int(g[0]), int(g[1] or 0), ampm)
        elif kind == "colon":
            t = _hour(int(g[0]), int(g[1]), None)
        elif kind == "at":
            t = _hour(int(g[0]), 0, None)
        else:  # part of day
            return _PART_ALIASES.get(g[0], g[0])
        if t is not None:
            return f"around {_hhmm(t)}"
    return None


def clock_times(text: str) -> list[time]:
    """Explicit clock times mentioned ('3 pm', '15:30', 'at 2'); used to match offered slots."""
    found: list[time] = []
    for kind in ("clock", "colon", "at"):
        pattern = dict(_TIME_PATTERNS)[kind]
        for m in pattern.finditer(text):
            g = m.groups()
            if kind == "clock":
                ampm = None if (g[2] or "").startswith("o") else g[2]
                t = _hour(int(g[0]), int(g[1] or 0), ampm)
            elif kind == "colon":
                t = _hour(int(g[0]), int(g[1]), None)
            else:
                t = _hour(int(g[0]), 0, None)
            if t is not None and t not in found:
                found.append(t)
    return found


# --------------------------------------------------------------------------- resolution


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


def window_for(text: str | None, business_hours: tuple[int, int] = (9, 18)) -> TimeWindow | None:
    """Time phrase → TimeWindow clamped to business hours.

    Accepts canonical phrases from `extract_time` and raw ones ('3 pm', 'after 4').
    Returns None when there is no usable time phrase. Raises PreferenceError("outside_hours")
    when the time cannot fall inside business hours.
    """
    if not text:
        return None
    canonical = extract_time(text.strip().lower())
    if canonical is None:
        return None
    open_m, close_m = business_hours[0] * 60, business_hours[1] * 60
    if canonical in WINDOWS:
        w = WINDOWS[canonical]
        start, end = max(_minutes(w.start), open_m), min(_minutes(w.end), close_m)
        label = w.label
    else:
        kind, *rest = canonical.split(" ")
        times = [time.fromisoformat(x) for x in rest if ":" in x]
        t = _minutes(times[0])
        if kind == "around":
            start, end = max(t - 60, open_m), min(t + 60, close_m)
            if not open_m <= t < close_m:
                raise PreferenceError("outside_hours")
            label = f"around {fmt_time(times[0])}"
        elif kind == "after":
            start, end = max(t, open_m), close_m
            label = f"after {fmt_time(times[0])}"
        elif kind == "before":
            start, end = open_m, min(t, close_m)
            label = f"before {fmt_time(times[0])}"
        else:  # between
            start, end = max(t, open_m), min(_minutes(times[1]), close_m)
            label = f"between {fmt_time(times[0])} and {fmt_time(times[1])}"
    if end - start < 30:  # not even one 30-minute slot fits
        raise PreferenceError("outside_hours")
    return TimeWindow(start=_time(start), end=_time(end), label=label)


@dataclass(frozen=True)
class RelativeDateResolver:
    horizon_days: int = 14
    business_hours: tuple[int, int] = (9, 18)
    lead_time_min: int = 120
    slot_minutes: int = 30

    def _bookable_today(self, ref: datetime) -> bool:
        now = to_ist(ref)
        earliest = now + timedelta(minutes=self.lead_time_min + self.slot_minutes)
        return earliest <= ist_datetime(now.date(), time(self.business_hours[1]))

    def resolve_date(self, date_text: str | None, ref: datetime) -> date | None:
        """Canonical or raw day phrase → date. None for 'any' or anything unrecognised.

        - bare weekday ⇒ next occurrence; today only if bookable hours are left today
        - 'next <weekday>' ⇒ that weekday in the following calendar week (Mon-Sun)
        - '7 oct' / 'oct 7' / '7/10' ⇒ this year, or next year if already past
        """
        if not date_text:
            return None
        text = date_text.strip().lower()
        canonical = extract_date(text)
        if canonical is None or canonical == ANY:
            return None
        today = to_ist(ref).date()
        for kind, pattern in _DATE_PATTERNS:
            m = pattern.search(canonical)
            if m is None:
                continue
            g = m.groups()
            if kind == "iso":
                return _safe_date(int(g[0]), int(g[1]), int(g[2]))
            if kind == "day_after":
                return today + timedelta(days=2)
            if kind == "relative":
                return today if g[0] == "today" else today + timedelta(days=1)
            if kind == "in_days":
                n = int(g[0]) if g[0].isdigit() else _SMALL_NUMBERS[g[0]]
                return today + timedelta(days=n)
            if kind == "next_week":
                return today + timedelta(days=7 - today.weekday())
            if kind == "weekday":
                wd = WEEKDAYS[g[1]]
                if g[0] == "next":
                    return today + timedelta(days=7 - today.weekday() + wd)
                delta = (wd - today.weekday()) % 7
                if delta == 0 and not self._bookable_today(ref):
                    delta = 7
                return today + timedelta(days=delta)
            if kind in ("day_month", "month_day"):
                day, mon = (g[0], g[1]) if kind == "day_month" else (g[1], g[0])
                return _upcoming(today, MONTHS[mon], int(day))
            if kind == "slash":
                year = g[2]
                if year:
                    y = int(year) + (2000 if len(year) == 2 else 0)
                    return _safe_date(y, int(g[1]), int(g[0]))
                return _upcoming(today, int(g[1]), int(g[0]))
            if kind == "ordinal":
                d = _safe_date(today.year, today.month, int(g[0]))
                if d is not None and d >= today:
                    return d
                nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                return _safe_date(nxt.year, nxt.month, int(g[0]))
        return None

    def check_date(self, d: date, ref: datetime) -> None:
        today = to_ist(ref).date()
        if d < today or d > today + timedelta(days=self.horizon_days):
            raise PreferenceError("out_of_range")
        if d.weekday() >= 5:
            raise PreferenceError("non_working_day")

    def resolve(self, date_text: str | None, time_text: str | None, *, ref: datetime
                ) -> Preference:  # fmt: skip
        """Full resolution with validation (raises PreferenceError)."""
        d = self.resolve_date(date_text, ref)
        if d is not None:
            self.check_date(d, ref)
        return Preference(date=d, window=window_for(time_text, self.business_hours))

    def canonical_time(self, time_text: str | None) -> str | None:
        return extract_time(time_text.strip().lower()) if time_text else None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _upcoming(today: date, month: int, day: int) -> date | None:
    d = _safe_date(today.year, month, day)
    if d is not None and d < today:
        d = _safe_date(today.year + 1, month, day)
    return d
