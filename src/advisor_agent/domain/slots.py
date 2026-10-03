"""Mock calendar and slot picker (LLD 1.3).

Pure, in-memory code: the caller loads the mock calendar JSON and passes it in.
SlotPicker.pick_two() is the plan's findTwoSlots / MockCalendarService.
"""

import random
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from advisor_agent.domain.models import Preference, Slot, TimeWindow
from advisor_agent.domain.timeutil import fmt_time, ist_datetime, to_ist, to_utc

Clock = Callable[[], datetime]


class SlotTaken(Exception):
    """The slot is already reserved (or does not exist)."""


class SlotRepository(Protocol):
    def free_slots(self, start_utc: datetime, end_utc: datetime) -> list[Slot]: ...
    def reserve(self, slot_id: str, code: str) -> None: ...
    def release(self, slot_id: str) -> None: ...


class InMemorySlotRepository:
    """Phase 1 repository. Reservations are an in-memory map guarded by a lock."""

    def __init__(self, slots: Iterable[Slot]) -> None:
        self._slots = {s.slot_id: s for s in sorted(slots, key=lambda s: s.start_utc)}
        self._reserved: dict[str, str] = {}
        self._lock = threading.Lock()

    def free_slots(self, start_utc: datetime, end_utc: datetime) -> list[Slot]:
        with self._lock:
            return [
                s
                for s in self._slots.values()
                if start_utc <= s.start_utc < end_utc and s.slot_id not in self._reserved
            ]

    def reserve(self, slot_id: str, code: str) -> None:
        with self._lock:
            if slot_id not in self._slots or slot_id in self._reserved:
                raise SlotTaken(slot_id)
            self._reserved[slot_id] = code

    def release(self, slot_id: str) -> None:
        with self._lock:
            self._reserved.pop(slot_id, None)


def _slot_id(start_ist: datetime) -> str:
    return f"S-{start_ist:%Y%m%d-%H%M}"


def make_slot(start: datetime, minutes: int = 30) -> Slot:
    """Build a slot from an aware start time (any zone)."""
    start_ist = to_ist(start)
    return Slot(
        slot_id=_slot_id(start_ist),
        start_utc=to_utc(start),
        end_utc=to_utc(start + timedelta(minutes=minutes)),
    )


def slots_from_mock_calendar(data: dict[str, Any]) -> list[Slot]:
    """Parse the `data/mock_calendar.json` structure (already loaded by the caller)."""
    minutes = int(data.get("slot_minutes", 30))
    slots = []
    for item in data.get("free_slots", []):
        start = datetime.fromisoformat(item["start"])
        slot = make_slot(start, minutes)
        if "slot_id" in item:
            slot = slot.model_copy(update={"slot_id": item["slot_id"]})
        slots.append(slot)
    return slots


def generate_free_slots(
    start: date,
    *,
    working_days: int = 14,
    seed: int = 42,
    density: float = 0.6,
    business_hours: tuple[int, int] = (9, 18),
    slot_minutes: int = 30,
) -> list[Slot]:
    """Deterministic mock availability: a random ~`density` share of business-hour slots
    on the next `working_days` weekdays starting at `start`."""
    rng = random.Random(seed)
    open_h, close_h = business_hours
    slots: list[Slot] = []
    day, produced = start, 0
    while produced < working_days:
        if day.weekday() < 5:
            t = ist_datetime(day, time(open_h))
            end = ist_datetime(day, time(close_h))
            while t + timedelta(minutes=slot_minutes) <= end:
                if rng.random() < density:
                    slots.append(make_slot(t, slot_minutes))
                t += timedelta(minutes=slot_minutes)
            produced += 1
        day += timedelta(days=1)
    return slots


class SlotPicker:
    def __init__(
        self,
        repo: SlotRepository,
        clock: Clock,
        *,
        horizon_days: int = 14,
        lead_time: timedelta = timedelta(hours=2),
        business_hours: tuple[int, int] = (9, 18),
        min_gap: timedelta = timedelta(minutes=60),
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._horizon_days = horizon_days
        self._lead_time = lead_time
        self._open = time(business_hours[0])
        self._close = time(business_hours[1])
        self._min_gap = min_gap

    def _today(self) -> date:
        return to_ist(self._clock()).date()

    def _horizon_end(self) -> date:
        return self._today() + timedelta(days=self._horizon_days)

    def _bookable(self, start_utc: datetime, end_utc: datetime) -> list[Slot]:
        earliest = self._clock() + self._lead_time
        result = []
        for s in self._repo.free_slots(start_utc, end_utc):
            start, end = to_ist(s.start_utc), to_ist(s.end_utc)
            if s.start_utc < earliest:
                continue
            if start.date().weekday() >= 5:
                continue
            if start.time() < self._open or end > ist_datetime(start.date(), self._close):
                continue
            result.append(s)
        return result

    def pick_two(self, pref: Preference, *, exclude: Iterable[str] = frozenset()) -> list[Slot]:
        """Return [], one slot, or exactly two slots for the preference.

        1. Candidates = free slots on pref.date within pref.window (or the whole horizon).
        2. Drop slots inside the lead time and anything in `exclude`.
        3. Sort by distance to the window midpoint (per day), then chronologically.
        4. Take the best slot plus the next one at least `min_gap` apart (if any), else the next.
        The two slots are returned in chronological order.
        """
        excluded = set(exclude)
        today, horizon_end = self._today(), self._horizon_end()
        if pref.date is not None:
            if not today <= pref.date <= horizon_end:
                return []
            first_day, last_day = pref.date, pref.date
        else:
            first_day, last_day = today, horizon_end
        start_utc = to_utc(ist_datetime(first_day, time(0)))
        end_utc = to_utc(ist_datetime(last_day + timedelta(days=1), time(0)))

        candidates = []
        for s in self._bookable(start_utc, end_utc):
            if s.slot_id in excluded:
                continue
            if pref.window is not None and not self._in_window(s, pref.window):
                continue
            candidates.append(s)

        candidates.sort(key=lambda s: self._rank(s, pref.window))
        if not candidates:
            return []
        first = candidates[0]
        rest = candidates[1:]
        far_enough = (s for s in rest if abs(s.start_utc - first.start_utc) >= self._min_gap)
        second = next(far_enough, None)
        if second is None and rest:
            second = rest[0]
        picked = [first] if second is None else [first, second]
        return sorted(picked, key=lambda s: s.start_utc)

    @staticmethod
    def _in_window(slot: Slot, window: TimeWindow) -> bool:
        start, end = to_ist(slot.start_utc), to_ist(slot.end_utc)
        return start.time() >= window.start and end <= ist_datetime(start.date(), window.end)

    @staticmethod
    def _rank(slot: Slot, window: TimeWindow | None) -> tuple[date, float, datetime]:
        start = to_ist(slot.start_utc)
        if window is None:
            return (start.date(), 0.0, slot.start_utc)
        w_start = ist_datetime(start.date(), window.start)
        w_end = ist_datetime(start.date(), window.end)
        midpoint = w_start + (w_end - w_start) / 2
        return (start.date(), abs((start - midpoint).total_seconds()), slot.start_utc)

    def availability_windows(self, date_from: date, days: int = 3) -> dict[date, list[TimeWindow]]:
        """Merged contiguous free ranges per day (check_availability intent, peek only)."""
        start_utc = to_utc(ist_datetime(date_from, time(0)))
        end_utc = to_utc(ist_datetime(date_from + timedelta(days=days), time(0)))
        result: dict[date, list[TimeWindow]] = {}
        ranges: list[tuple[datetime, datetime]] = []
        for s in sorted(self._bookable(start_utc, end_utc), key=lambda s: s.start_utc):
            start, end = to_ist(s.start_utc), to_ist(s.end_utc)
            if ranges and ranges[-1][1] == start:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        for start, end in ranges:
            label = f"{fmt_time(start.time())} to {fmt_time(end.time())}"
            result.setdefault(start.date(), []).append(
                TimeWindow(start=start.time(), end=end.time(), label=label)
            )
        return result
