from datetime import UTC, date, datetime

import pytest

from advisor_agent.domain.models import Preference, Slot, TimeWindow
from advisor_agent.domain.timeutil import fmt_date, fmt_preference, fmt_slot, to_ist, to_utc


def _slot(start_utc: datetime) -> Slot:
    return Slot(slot_id="x", start_utc=start_utc, end_utc=start_utc)


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 10, 6, 8, 30, tzinfo=UTC), "Tuesday, 6 October 2026, 2:00 PM IST"),
        (datetime(2026, 10, 6, 4, 0, tzinfo=UTC), "Tuesday, 6 October 2026, 9:30 AM IST"),
        (datetime(2026, 10, 6, 6, 30, tzinfo=UTC), "Tuesday, 6 October 2026, 12:00 PM IST"),
        # 19:00 UTC on Monday rolls into Tuesday 00:30 IST
        (datetime(2026, 10, 5, 19, 0, tzinfo=UTC), "Tuesday, 6 October 2026, 12:30 AM IST"),
    ],
)
def test_fmt_slot(utc, expected):
    out = fmt_slot(_slot(utc))
    assert out == expected
    assert out.endswith("IST")


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 10, 6, 14, 0))  # noqa: DTZ001
    with pytest.raises(ValueError):
        to_ist(datetime(2026, 10, 6, 14, 0))  # noqa: DTZ001
    with pytest.raises(ValueError):
        Slot(slot_id="x", start_utc=datetime(2026, 10, 6), end_utc=datetime(2026, 10, 6))  # noqa: DTZ001


def test_fmt_date_and_preference():
    assert fmt_date(date(2026, 10, 6)) == "Tuesday, 6 October 2026"
    w = TimeWindow(start=datetime(2026, 1, 1, 12).time(), end=datetime(2026, 1, 1, 16).time(),  # noqa: DTZ001
                   label="afternoon")
    assert fmt_preference(Preference(date=date(2026, 10, 6), window=w)) == (
        "Tuesday, 6 October 2026 (afternoon)"
    )
    assert fmt_preference(Preference()) == "any working day"
