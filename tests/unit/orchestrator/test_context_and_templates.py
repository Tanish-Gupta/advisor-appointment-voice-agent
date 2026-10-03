import re
from datetime import date, datetime, time, timedelta

import pytest

from advisor_agent.domain.models import Preference, Topic
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.schema import NLUResult
from advisor_agent.orchestrator.context import SessionContext
from advisor_agent.orchestrator.preference import WINDOWS, PreferenceError, preference_from_nlu
from advisor_agent.orchestrator.session_store import InMemorySessionStore
from advisor_agent.orchestrator.templates import ALLOWED_PARAMS, load_templates, render

TODAY = date(2026, 10, 2)  # Friday


def test_preference_mapping():
    pref = preference_from_nlu(
        NLUResult(date_iso=date(2026, 10, 6), time_text="afternoon"), TODAY, 14
    )
    assert pref == Preference(date=date(2026, 10, 6), window=WINDOWS["afternoon"])
    assert WINDOWS["afternoon"].start == time(12) and WINDOWS["afternoon"].end == time(16)


@pytest.mark.parametrize(
    ("d", "reason"),
    [
        (date(2026, 10, 1), "out_of_range"),
        (TODAY + timedelta(days=15), "out_of_range"),
        (date(2026, 10, 3), "non_working_day"),
        (date(2026, 10, 4), "non_working_day"),
    ],
)
def test_preference_errors(d, reason):
    with pytest.raises(PreferenceError) as e:
        preference_from_nlu(NLUResult(date_iso=d), TODAY, 14)
    assert e.value.reason == reason


def test_session_store_ttl_and_copies():
    now = [datetime(2026, 10, 2, 10, tzinfo=IST)]
    store = InMemorySessionStore(timedelta(minutes=30), lambda: now[0])
    ctx = SessionContext(session_id="s1")
    store.save(ctx)
    loaded = store.load("s1")
    assert loaded is not None and loaded is not ctx
    loaded.turn_no = 99
    assert store.load("s1").turn_no == 0  # mutations don't leak without save
    now[0] += timedelta(minutes=31)
    assert store.load("s1") is None


def test_all_templates_render_with_full_params():
    ctx = SessionContext(session_id="s", topic=Topic.SIP_MANDATES)
    extra = {p: "x" for p in ALLOWED_PARAMS}
    for tid in load_templates():
        assert render([tid], ctx, extra)[0]


def test_render_rejects_non_whitelisted_params():
    with pytest.raises(ValueError):
        render(["GREET"], SessionContext(session_id="s"), {"free_text": "hi"})


def test_render_missing_param_fails_loudly():
    with pytest.raises(KeyError):
        render(["TOPIC_ACK"], SessionContext(session_id="s"))


def test_templates_are_speakable():
    """Bold-only markdown; URLs only in SECURE_LINK (LLD 2.9)."""
    for tid, text in load_templates().items():
        stripped = re.sub(r"\{\w+\}", "", text).replace("**", "")
        assert not any(ch in stripped for ch in "*_#`[]|"), tid
        if tid != "SECURE_LINK":
            assert "http" not in text, tid
