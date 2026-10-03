"""The Google API wrappers used by the FastMCP server, against fake discovery services."""

from email.header import decode_header, make_header

import pytest

from advisor_agent.mcp_server.google_calendar import deterministic_event_id, hold_title
from advisor_agent.mcp_server.google_docs import prebooking_line
from advisor_agent.mcp_server.google_gmail import render
from tests.fake_google import FakeGoogle, http_error

START = "2026-10-06T13:00:00+05:30"
END = "2026-10-06T13:30:00+05:30"


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


def _hold(g: FakeGoogle, **kw: object) -> dict:
    args = {"code": "NL-A742", "topic": "SIP/Mandates", "start_ist": START, "end_ist": END,
            "kind": "booking", "version": 1, **kw}  # fmt: skip
    return g.cal_api.create_hold(**args)


def test_event_id_is_deterministic_and_valid_base32hex() -> None:
    a = deterministic_event_id("NL-A742", "booking", 1)
    assert a == deterministic_event_id("NL-A742", "booking", 1)
    assert a != deterministic_event_id("NL-A742", "booking", 2)
    assert 5 <= len(a) <= 1024 and set(a) <= set("0123456789abcdefghijklmnopqrstuv")


def test_hold_title_matches_spec() -> None:
    assert hold_title("SIP/Mandates", "NL-A742") == "Advisor Q&A \u2014 SIP/Mandates \u2014 NL-A742"


def test_create_hold_is_tentative_private_and_ist(g: FakeGoogle) -> None:
    res = _hold(g)
    assert res["created"] is True
    ev = g.calendar.events_by_id[res["event_id"]]
    assert ev["summary"] == "Advisor Q&A \u2014 SIP/Mandates \u2014 NL-A742"
    assert ev["status"] == "tentative" and ev["visibility"] == "private"
    assert ev["start"] == {"dateTime": START, "timeZone": "Asia/Kolkata"}
    assert "attendees" not in ev  # nobody is invited, so no email is ever sent by Calendar


def test_create_hold_retry_is_idempotent(g: FakeGoogle) -> None:
    first = _hold(g)
    second = _hold(g)
    assert second == {**first, "created": False}
    assert len(g.calendar.live_events()) == 1


def test_create_hold_revives_a_cancelled_event(g: FakeGoogle) -> None:
    res = _hold(g)
    g.cal_api.delete_hold(res["event_id"])
    assert g.calendar.live_events() == []
    _hold(g)
    assert len(g.calendar.live_events()) == 1


def test_create_hold_propagates_other_errors(g: FakeGoogle) -> None:
    g.calendar.fail = [503]
    with pytest.raises(Exception) as e:
        _hold(g)
    assert e.value.resp.status == 503  # type: ignore[attr-defined]


def test_delete_hold_is_idempotent(g: FakeGoogle) -> None:
    res = _hold(g)
    assert g.cal_api.delete_hold(res["event_id"]) is True
    assert g.cal_api.delete_hold(res["event_id"]) is True  # 410 -> already gone
    assert g.cal_api.delete_hold("doesnotexist1") is True


def test_list_busy_queries_ist(g: FakeGoogle) -> None:
    g.calendar.external_busy = [{"start": "2026-10-06T07:30:00Z", "end": "2026-10-06T08:00:00Z"}]
    busy = g.cal_api.list_busy(START, END)
    assert busy == [{"start": "2026-10-06T07:30:00Z", "end": "2026-10-06T08:00:00Z"}]
    assert g.calendar.last_freebusy_body["timeZone"] == "Asia/Kolkata"


def test_docs_append_and_dedupe(g: FakeGoogle) -> None:
    args = {"date": "2026-10-02", "topic": "SIP/Mandates", "slot": "Tue, 6 Oct, 1:00 PM IST",
            "code": "NL-A742", "status": "tentative"}  # fmt: skip
    assert g.docs_api.append_prebooking(**args)["appended"] is True
    assert g.docs_api.append_prebooking(**args)["appended"] is False  # retry: no duplicate
    other = {**args, "code": "NL-B100"}
    g.docs_api.append_prebooking(**other)
    assert g.docs.lines() == [prebooking_line(**args), prebooking_line(**other)]


def test_docs_append_to_existing_heading(g: FakeGoogle) -> None:
    g.docs.text = "Advisor Pre-Bookings"
    g.docs_api.append_prebooking(date="2026-10-02", topic="KYC/Onboarding", slot="s",
                                 code="NL-K001", status="tentative")  # fmt: skip
    assert g.docs.lines() == [
        "Advisor Pre-Bookings",
        "2026-10-02 | KYC/Onboarding | s | NL-K001 | tentative",
    ]


def test_gmail_creates_a_draft_to_the_fixed_advisor(g: FakeGoogle) -> None:
    res = g.gmail_api.create_draft(template="booking", code="NL-A742", topic="SIP/Mandates",
                                   slot="Tue, 6 Oct, 1:00 PM IST")  # fmt: skip
    assert res["draft_id"] == "draft-1"
    msg = g.gmail.drafts_created[0]
    assert msg["To"] == "advisor@example.com"
    subject = str(make_header(decode_header(msg["Subject"])))
    assert subject == "[Pre-booking] SIP/Mandates \u2014 NL-A742"
    assert "Tue, 6 Oct, 1:00 PM IST" in msg.get_payload(decode=True).decode()
    assert g.gmail.sent == 0


def test_gmail_unknown_template_rejected() -> None:
    with pytest.raises(ValueError):
        render("send_money", code="NL-A742", topic="x", slot=None)


def test_http_error_helper_status() -> None:
    assert http_error(429).resp.status == 429
