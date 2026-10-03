"""RelativeDateResolver + time windows (LLD 3.4). Reference: Friday 2 Oct 2026, 16:00 IST."""

from datetime import date, datetime, time

import pytest

from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.date_resolver import (
    ANY,
    PreferenceError,
    RelativeDateResolver,
    clock_times,
    extract_date,
    extract_time,
    window_for,
)

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)  # Friday; too late to book today
MORNING_REF = datetime(2026, 10, 2, 9, 0, tzinfo=IST)  # Friday; today is still bookable
R = RelativeDateResolver()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("friday", date(2026, 10, 9)),  # today has no bookable hours left -> next week
        ("Friday", date(2026, 10, 9)),
        ("tuesday", date(2026, 10, 6)),
        ("tue", date(2026, 10, 6)),
        ("this thursday", date(2026, 10, 8)),
        ("on monday", date(2026, 10, 5)),
        ("next tuesday", date(2026, 10, 6)),  # following calendar week
        ("next friday", date(2026, 10, 9)),
        ("tuesday next week", date(2026, 10, 6)),
        ("next week", date(2026, 10, 5)),  # next Monday
        ("today", date(2026, 10, 2)),
        ("tonight", date(2026, 10, 2)),
        ("tomorrow", date(2026, 10, 3)),
        ("day after tomorrow", date(2026, 10, 4)),
        ("in 3 days", date(2026, 10, 5)),
        ("in two days", date(2026, 10, 4)),
        ("7 oct", date(2026, 10, 7)),
        ("7th of october", date(2026, 10, 7)),
        ("oct 7th", date(2026, 10, 7)),
        ("07/10", date(2026, 10, 7)),  # Indian dd/mm order
        ("7/10/2026", date(2026, 10, 7)),
        ("2026-10-08", date(2026, 10, 8)),
        ("the 7th", date(2026, 10, 7)),
        ("the 1st", date(2026, 11, 1)),  # already past this month -> next month
        ("1 oct", date(2027, 10, 1)),  # already past this year -> next year
    ],
)
def test_resolve_date(text, expected):
    assert R.resolve_date(text, REF) == expected


def test_same_weekday_counts_when_hours_left_today():
    assert R.resolve_date("friday", MORNING_REF) == date(2026, 10, 2)


@pytest.mark.parametrize("text", [None, "", "any", "whenever", "blue", "31/02"])
def test_resolve_date_none(text):
    assert R.resolve_date(text, REF) is None


@pytest.mark.parametrize(
    "text", ["anytime", "any time", "whenever", "earliest available", "asap", "either",
             "any", "doesn't matter", "i'm flexible", "no preference", "first available"],
)  # fmt: skip
def test_any_phrases(text):
    assert extract_date(text) == ANY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tuesday afternoon", "tuesday"),
        ("tuesday next week", "next tuesday"),
        ("next wed", "next wed"),
        ("can we do tomorrow", "tomorrow"),
        ("hello there", None),
    ],
)
def test_extract_date(text, expected):
    assert extract_date(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("afternoon", "afternoon"),
        ("early morning", "morning"),
        ("tonight", "evening"),
        ("3 pm", "around 15:00"),
        ("at 2", "around 14:00"),
        ("around 11", "around 11:00"),
        ("3:30", "around 15:30"),
        ("4 o'clock", "around 16:00"),
        ("noon", "around 12:00"),
        ("after lunch", "after 13:00"),
        ("after 4", "after 16:00"),
        ("before 11", "before 11:00"),
        ("between 2 and 4", "between 14:00 and 16:00"),
        ("from 2 to 4 pm", "between 14:00 and 16:00"),
        ("in 3 days", None),
        ("blue", None),
    ],
)
def test_extract_time(text, expected):
    assert extract_time(text) == expected


@pytest.mark.parametrize(
    ("text", "start", "end", "label"),
    [
        ("morning", time(9), time(12), "morning"),
        ("afternoon", time(12), time(16), "afternoon"),
        ("evening", time(16), time(18), "evening"),
        ("after 4", time(16), time(18), "after 4:00 PM"),
        ("around 3 pm", time(14), time(16), "around 3:00 PM"),
        ("9 am", time(9), time(10), "around 9:00 AM"),  # clamped to opening time
        ("before 11", time(9), time(11), "before 11:00 AM"),
        ("between 2 and 4", time(14), time(16), "between 2:00 PM and 4:00 PM"),
        ("noon", time(11), time(13), "around 12:00 PM"),
    ],
)
def test_window_for(text, start, end, label):
    w = window_for(text)
    assert w is not None
    assert (w.start, w.end, w.label) == (start, end, label)


@pytest.mark.parametrize("text", ["8 am", "7 pm", "after 6 pm", "before 9 am"])
def test_window_outside_hours(text):
    with pytest.raises(PreferenceError) as e:
        window_for(text)
    assert e.value.reason == "outside_hours"


@pytest.mark.parametrize("text", [None, "", "whenever", "blue"])
def test_window_none(text):
    assert window_for(text) is None


def test_resolve_full_preference():
    pref = R.resolve("next tuesday", "after 3 pm", ref=REF)
    assert pref.date == date(2026, 10, 6)
    assert pref.window is not None and pref.window.start == time(15)


@pytest.mark.parametrize(
    ("date_text", "reason"),
    [("tomorrow", "non_working_day"), ("sunday", "non_working_day"),
     ("20 oct", "out_of_range")],
)  # fmt: skip
def test_resolve_rejects(date_text, reason):
    with pytest.raises(PreferenceError) as e:
        R.resolve(date_text, None, ref=REF)
    assert e.value.reason == reason


def test_resolve_any_day():
    pref = R.resolve("any", "morning", ref=REF)
    assert pref.date is None and pref.window is not None


def test_clock_times():
    assert clock_times("3 pm or 15:30") == [time(15), time(15, 30)]
    assert clock_times("the tuesday one") == []


def test_canonical_time():
    assert R.canonical_time("After 4 PM") == "after 16:00"
    assert R.canonical_time(None) is None
