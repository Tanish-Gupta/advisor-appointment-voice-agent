"""NLU date/time fields -> domain Preference, with range checks (LLD 3.4).

The vocabulary (WINDOWS, window_for, PreferenceError) lives in nlu.date_resolver since Phase 3;
it is re-exported here for the orchestrator and existing tests.
"""

from datetime import date, timedelta

from advisor_agent.domain.models import Preference
from advisor_agent.nlu.date_resolver import WINDOWS, PreferenceError, window_for
from advisor_agent.nlu.schema import NLUResult

__all__ = ["WINDOWS", "PreferenceError", "has_preference", "preference_from_nlu"]


def has_preference(nlu: NLUResult) -> bool:
    return bool(nlu.date_text or nlu.date_iso or nlu.time_text)


def preference_from_nlu(
    nlu: NLUResult,
    today: date,
    horizon_days: int,
    business_hours: tuple[int, int] = (9, 18),
) -> Preference:
    """Raises PreferenceError: out_of_range | non_working_day | outside_hours."""
    window = window_for(nlu.time_text, business_hours)
    d = nlu.date_iso
    if d is not None:
        if d < today or d > today + timedelta(days=horizon_days):
            raise PreferenceError("out_of_range")
        if d.weekday() >= 5:
            raise PreferenceError("non_working_day")
    return Preference(date=d, window=window)
