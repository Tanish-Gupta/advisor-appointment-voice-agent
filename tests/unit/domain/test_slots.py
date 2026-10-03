from datetime import date, datetime, time, timedelta

import pytest

from advisor_agent.domain.models import Preference, TimeWindow
from advisor_agent.domain.slots import (
    InMemorySlotRepository,
    SlotPicker,
    SlotTaken,
    generate_free_slots,
    make_slot,
    slots_from_mock_calendar,
)
from advisor_agent.domain.timeutil import IST, to_ist

AFTERNOON = TimeWindow(start=time(12), end=time(16), label="afternoon")
MORNING = TimeWindow(start=time(9), end=time(12), label="morning")
TUE = date(2026, 10, 6)
NOW = datetime(2026, 10, 2, 16, 0, tzinfo=IST)  # Friday


def ist(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def picker(starts: list[datetime], now: datetime = NOW, **kw) -> SlotPicker:
    repo = InMemorySlotRepository(make_slot(s) for s in starts)
    return SlotPicker(repo, lambda: now, **kw)


def hours(slots) -> list[str]:
    return [f"{to_ist(s.start_utc):%a %H:%M}" for s in slots]


def test_empty_calendar_returns_empty():
    assert picker([]).pick_two(Preference(date=TUE, window=AFTERNOON)) == []


def test_single_slot_returned():
    out = picker([ist(TUE, 14)]).pick_two(Preference(date=TUE, window=AFTERNOON))
    assert hours(out) == ["Tue 14:00"]


def test_many_slots_truncated_to_exactly_two_and_spaced():
    starts = [ist(TUE, 12), ist(TUE, 13), ist(TUE, 13, 30), ist(TUE, 14), ist(TUE, 15)]
    out = picker(starts).pick_two(Preference(date=TUE, window=AFTERNOON))
    assert len(out) == 2
    assert out[1].start_utc - out[0].start_utc >= timedelta(minutes=60)
    assert out[0].start_utc < out[1].start_utc


def test_closest_to_window_midpoint_first():
    starts = [ist(TUE, 12), ist(TUE, 14), ist(TUE, 15, 30)]
    out = picker(starts).pick_two(Preference(date=TUE, window=AFTERNOON))
    assert "Tue 14:00" in hours(out)


def test_falls_back_to_adjacent_slot_when_no_gap_possible():
    out = picker([ist(TUE, 14), ist(TUE, 14, 30)]).pick_two(Preference(date=TUE, window=AFTERNOON))
    assert hours(out) == ["Tue 14:00", "Tue 14:30"]


def test_window_filter_excludes_slots_ending_after_window():
    p = picker([ist(TUE, 15, 45), ist(TUE, 16, 30)])
    out = p.pick_two(Preference(date=TUE, window=AFTERNOON))
    assert out == []


def test_excluded_slots_are_skipped():
    starts = [ist(TUE, 13), ist(TUE, 14), ist(TUE, 15)]
    p = picker(starts)
    first = p.pick_two(Preference(date=TUE, window=AFTERNOON))
    second = p.pick_two(Preference(date=TUE, window=AFTERNOON), exclude={s.slot_id for s in first})
    assert not {s.slot_id for s in first} & {s.slot_id for s in second}
    assert len(second) == 1


def test_today_after_business_hours_is_empty():
    late = datetime(2026, 10, 6, 19, 0, tzinfo=IST)
    assert picker([ist(TUE, 14), ist(TUE, 17)], now=late).pick_two(Preference(date=TUE)) == []


def test_lead_time_of_two_hours():
    now = ist(TUE, 13)
    out = picker([ist(TUE, 14), ist(TUE, 15), ist(TUE, 16)], now=now).pick_two(Preference(date=TUE))
    assert hours(out) == ["Tue 15:00", "Tue 16:00"]


def test_weekend_is_empty():
    sat = date(2026, 10, 10)
    assert picker([ist(sat, 11)]).pick_two(Preference(date=sat)) == []


def test_horizon_boundaries():
    last_day = date(2026, 10, 16)  # NOW + 14 days
    beyond = date(2026, 10, 19)
    p = picker([ist(last_day, 11), ist(beyond, 11)])
    assert hours(p.pick_two(Preference(date=last_day))) == ["Fri 11:00"]
    assert p.pick_two(Preference(date=beyond)) == []
    assert hours(p.pick_two(Preference())) == ["Fri 11:00"]


def test_past_date_is_empty():
    assert picker([ist(date(2026, 10, 1), 11)]).pick_two(Preference(date=date(2026, 10, 1))) == []


def test_any_day_prefers_earliest_day():
    wed = date(2026, 10, 7)
    out = picker([ist(wed, 10), ist(TUE, 10), ist(TUE, 15)]).pick_two(Preference(window=MORNING))
    assert hours(out) == ["Tue 10:00", "Wed 10:00"]


def test_reserve_and_release():
    s = make_slot(ist(TUE, 14))
    repo = InMemorySlotRepository([s])
    repo.reserve(s.slot_id, "NL-A742")
    with pytest.raises(SlotTaken):
        repo.reserve(s.slot_id, "NL-B234")
    assert repo.free_slots(s.start_utc, s.end_utc) == []
    repo.release(s.slot_id)
    assert repo.free_slots(s.start_utc, s.end_utc) == [s]


def test_reserve_unknown_slot():
    with pytest.raises(SlotTaken):
        InMemorySlotRepository([]).reserve("S-nope", "NL-A742")


def test_availability_windows_merges_contiguous_slots():
    starts = [ist(TUE, 10), ist(TUE, 10, 30), ist(TUE, 14)]
    windows = picker(starts).availability_windows(TUE, days=1)
    assert [w.label for w in windows[TUE]] == ["10:00 AM to 11:00 AM", "2:00 PM to 2:30 PM"]


def test_mock_calendar_parsing():
    data = {
        "slot_minutes": 30,
        "free_slots": [{"slot_id": "S-20261006-1400", "start": "2026-10-06T14:00:00+05:30"}],
    }
    [slot] = slots_from_mock_calendar(data)
    assert slot.slot_id == "S-20261006-1400"
    assert slot.end_utc - slot.start_utc == timedelta(minutes=30)


def test_generator_is_deterministic_and_weekday_only():
    a = generate_free_slots(date(2026, 10, 2), working_days=5, seed=7)
    b = generate_free_slots(date(2026, 10, 2), working_days=5, seed=7)
    assert a == b and a
    assert all(to_ist(s.start_utc).weekday() < 5 for s in a)
    assert all(time(9) <= to_ist(s.start_utc).time() < time(18) for s in a)
