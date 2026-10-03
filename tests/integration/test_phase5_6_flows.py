"""Phases 5-6 end to end through the chat service (LLD 5.4, 6.5).

Real: orchestrator, NLU (stub), ToolGate, FastMCP server, Google wrappers, SQLite store,
outbox and secure links. Fake: only Google's HTTP layer (tests/fake_google.py). The tool
calls come from the deterministic plan (no LLM), exactly as in production when Gemini is off.
"""

import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import pytest

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import ChatService
from advisor_agent.domain.models import BookingStatus, Slot
from advisor_agent.domain.timeutil import to_ist
from tests.conftest import default_slots, fixed_clock, make_settings
from tests.fake_google import FakeGoogle

URL_RE = re.compile(r"https?://\S+")
CODE_RE = re.compile(r"\bNL-[A-Z]\d{3}\b")
FULL_SLOT = r"\w+day, \d{1,2} \w+ \d{4}, \d{1,2}:\d{2} (?:AM|PM) IST"
TUESDAY = date(2026, 10, 6)


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


async def _service(g: FakeGoogle, slots: list[Slot] | None = None, **settings: Any) -> ChatService:
    svc = build_chat_service(
        make_settings(**settings),
        slots=default_slots() if slots is None else slots,
        clock=fixed_clock,
        tool_target=g.server(),
    )
    assert svc.runtime is not None
    await svc.runtime.start(run_worker=False)
    return svc


async def _say(svc: ChatService, sid: str | None, *texts: str):  # type: ignore[no-untyped-def]
    reply = None
    if sid is None:
        reply = await svc.start()
        sid = reply.session_id
    for t in texts:
        reply = await svc.send(sid, t)
    return sid, reply


def _ids(svc: ChatService) -> list[str]:
    assert svc.last_turn is not None
    return svc.last_turn.template_ids


def _text(reply: Any) -> str:
    return "\n".join(reply.messages)


async def _book(svc: ChatService, when: str = "time tuesday afternoon") -> str:
    _, reply = await _say(svc, None, "yes", "book", "topic sip", when, "1", "yes")
    code = CODE_RE.search(_text(reply)).group(0)
    await svc.runtime.worker.drain()
    return code


def _no_tuesday() -> list[Slot]:
    return [s for s in default_slots() if to_ist(s.start_utc).date() != TUESDAY]


# --- Phase 5: waitlist ---------------------------------------------------------------------


async def test_no_match_creates_waitlist_hold_notes_and_draft(g: FakeGoogle) -> None:
    svc = await _service(g, _no_tuesday())
    _, reply = await _say(svc, None, "yes", "book", "topic sip", "time tuesday afternoon")
    text = _text(reply)
    assert svc.last_turn.state == "close" and reply.done
    assert _ids(svc) == ["NO_MATCH_WAITLIST", "READ_WAITLIST_CODE", "SECURE_LINK", "GOODBYE"]
    assert "Tuesday, 6 October 2026 (afternoon) (IST)" in text
    code = CODE_RE.search(text).group(0)
    assert urlsplit(URL_RE.search(text).group(0)).path == f"/b/{code}"

    assert await svc.runtime.worker.drain() == 3
    (event,) = g.calendar.live_events()
    assert event["summary"] == f"Advisor Q&A \u2014 Waitlist \u2014 SIP/Mandates \u2014 {code}"
    assert event["transparency"] == "transparent"
    assert event["start"]["dateTime"].startswith("2026-10-06T12:00:00")
    (line,) = g.docs.lines()
    assert line.endswith(f"| {code} | waitlist") and "Tuesday, 6 October 2026" in line
    (draft,) = g.gmail.drafts_created
    assert code in str(draft["Subject"])
    booking = svc.runtime.store.get_booking(code)
    assert booking.status is BookingStatus.WAITLIST and booking.slot is None
    await svc.runtime.stop()


async def test_waitlist_notes_only_mode(g: FakeGoogle) -> None:
    svc = await _service(g, _no_tuesday(), waitlist_mode="notes_only")
    _, reply = await _say(svc, None, "yes", "book", "topic kyc", "time tuesday")
    assert _ids(svc)[0] == "NO_MATCH_WAITLIST"
    assert await svc.runtime.worker.drain() == 2
    assert g.calendar.live_events() == []
    assert g.docs.lines()[0].endswith("| waitlist") and len(g.gmail.drafts_created) == 1
    await svc.runtime.stop()


async def test_no_match_without_tools_still_asks_for_another_day() -> None:
    from advisor_agent.channels.chat.bootstrap import build_chat_service as build

    svc = build(make_settings(), slots=_no_tuesday(), clock=fixed_clock)
    _, reply = await _say(svc, None, "yes", "book", "topic sip", "time tuesday afternoon")
    assert svc.last_turn.state == "collect_pref"
    assert _ids(svc) == ["NO_MATCH_TRY_OTHER"]


# --- Phase 5: advice refusal ---------------------------------------------------------------


async def test_advice_mid_flow_refuses_and_resumes_with_same_slots(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid, offer = await _say(svc, None, "yes", "book", "topic sip", "time tuesday afternoon")
    offered = re.findall(FULL_SLOT, _text(offer))
    assert len(offered) == 2

    _, reply = await _say(svc, sid, "Which fund gives the best returns?")
    assert svc.last_turn.state == "await_pivot"
    assert _ids(svc) == ["ADVICE_REFUSAL", "EDU_LINKS", "PIVOT_OFFER"]
    assert "https://investor.sebi.gov.in" in _text(reply)
    assert re.findall(FULL_SLOT, _text(reply)) == []

    _, reply = await _say(svc, sid, "should I buy gold?")
    assert _ids(svc) == ["ADVICE_REFUSAL_SHORT", "PIVOT_OFFER"]

    _, reply = await _say(svc, sid, "yes")
    assert svc.last_turn.state == "offer_slots"
    assert re.findall(FULL_SLOT, _text(reply)) == offered

    _, reply = await _say(svc, sid, "which stock should i buy")
    _, reply = await _say(svc, sid, "2")  # answered as the interrupted question
    assert svc.last_turn.state == "confirm_slot"
    assert re.findall(FULL_SLOT, _text(reply)) == [offered[1]]

    _, reply = await _say(svc, sid, "yes")
    assert svc.last_turn.state == "close"
    assert offered[1] in _text(reply)  # IST date/time repeated on the final confirmation
    assert svc.runtime.runner.decisions  # still booked through the gated MCP tools
    await svc.runtime.stop()


async def test_advice_before_disclaimer_keeps_the_gate(g: FakeGoogle) -> None:
    svc = await _service(g)
    _, reply = await _say(svc, None, "should I invest in crypto?")
    assert svc.last_turn.state == "disclaimer_ack"
    assert _ids(svc) == ["ADVICE_REFUSAL", "EDU_LINKS", "DISCLAIMER_ASK_ACK"]
    await svc.runtime.stop()


async def test_advice_at_intent_detect_offers_a_booking(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid, _ = await _say(svc, None, "yes", "is now a good time to buy?")
    assert _ids(svc) == ["ADVICE_REFUSAL", "EDU_LINKS", "PIVOT_OFFER_NEW"]
    _, reply = await _say(svc, sid, "yes")
    assert svc.last_turn.state == "topic_confirm"
    _, reply = await _say(svc, sid, "stop")
    assert svc.last_turn.state == "close"
    await svc.runtime.stop()


async def test_advice_pivot_no_closes(g: FakeGoogle) -> None:
    svc = await _service(g)
    _, reply = await _say(svc, None, "yes", "book", "topic sip", "best mutual funds?", "no")
    assert svc.last_turn.state == "close" and _ids(svc) == ["GOODBYE"]
    await svc.runtime.stop()


# --- Phase 6: reschedule ------------------------------------------------------------------


async def test_reschedule_moves_hold_and_keeps_code(g: FakeGoogle) -> None:
    svc = await _service(g)
    code = await _book(svc)
    (old_event,) = g.calendar.live_events()

    sid, reply = await _say(svc, None, "yes", f"reschedule {code}")
    assert svc.last_turn.state == "collect_pref"
    assert _ids(svc) == ["CODE_FOUND", "ASK_NEW_PREF"]
    assert re.search(FULL_SLOT, _text(reply))  # the current slot is read back

    _, reply = await _say(svc, sid, "time wednesday morning", "1")
    assert svc.last_turn.state == "confirm_slot" and _ids(svc) == ["RESCHEDULE_READBACK"]
    assert len(re.findall(FULL_SLOT, _text(reply))) == 2  # from ... to ...
    new_label = re.findall(FULL_SLOT, _text(reply))[1]
    assert new_label.startswith("Wednesday, 7 October 2026")

    _, reply = await _say(svc, sid, "yes")
    text = _text(reply)
    assert svc.last_turn.state == "close"
    assert _ids(svc) == ["RESCHEDULED", "READ_CODE", "SECURE_LINK", "GOODBYE"]
    assert code in text and new_label in text and URL_RE.search(text)

    assert await svc.runtime.worker.drain() == 4  # create new, delete old, notes, draft
    (event,) = g.calendar.live_events()
    assert event["id"] != old_event["id"] and event["summary"].endswith(code)
    assert event["start"]["dateTime"].startswith("2026-10-07")
    assert g.docs.lines()[-1].endswith(f"| {code} | rescheduled")
    assert len(g.gmail.drafts_created) == 2
    booking = svc.runtime.store.get_booking(code)
    assert booking.version == 2 and booking.status is BookingStatus.TENTATIVE
    assert booking.calendar_event_id == event["id"]
    await svc.runtime.stop()


async def test_reschedule_unknown_code_twice_offers_new_booking(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid, _ = await _say(svc, None, "yes", "reschedule")
    assert svc.last_turn.state == "ask_code" and _ids(svc) == ["ASK_CODE"]
    _, reply = await _say(svc, sid, "NL-Z999")
    assert _ids(svc) == ["CODE_NOT_FOUND"] and "NL-Z999" in _text(reply)
    _, reply = await _say(svc, sid, "NL-Z998")
    assert svc.last_turn.state == "intent_detect"
    assert _ids(svc) == ["CODE_NOT_FOUND_GIVE_UP", "OFFER_TO_BOOK"]
    _, reply = await _say(svc, sid, "yes")
    assert svc.last_turn.state == "topic_confirm"
    await svc.runtime.stop()


async def test_reschedule_waitlist_code_is_explained(g: FakeGoogle) -> None:
    svc = await _service(g, _no_tuesday())
    _, reply = await _say(svc, None, "yes", "book", "topic sip", "time tuesday afternoon")
    code = CODE_RE.search(_text(reply)).group(0)
    _, reply = await _say(svc, None, "yes", f"reschedule {code}")
    assert _ids(svc) == ["WAITLIST_NO_RESCHEDULE", "OFFER_TO_BOOK"]
    await svc.runtime.stop()


# --- Phase 6: cancel ----------------------------------------------------------------------


async def test_cancel_deletes_hold_and_logs(g: FakeGoogle) -> None:
    svc = await _service(g)
    code = await _book(svc)
    sid, reply = await _say(svc, None, "yes", f"cancel {code}")
    assert svc.last_turn.state == "cancel_confirm" and _ids(svc) == ["CANCEL_READBACK"]
    assert re.search(FULL_SLOT, _text(reply))

    _, reply = await _say(svc, sid, "yes")
    assert svc.last_turn.state == "close" and _ids(svc) == ["CANCELLED", "GOODBYE"]
    assert URL_RE.search(_text(reply)) is None  # no secure link needed for a cancellation

    assert await svc.runtime.worker.drain() == 3  # delete hold, notes, draft
    assert g.calendar.live_events() == []
    assert g.docs.lines()[-1].endswith(f"| {code} | cancelled")
    assert len(g.gmail.drafts_created) == 2
    assert svc.runtime.store.get_booking(code).status is BookingStatus.CANCELLED

    _, reply = await _say(svc, None, "yes", f"reschedule {code}")
    assert _ids(svc) == ["CODE_CANCELLED", "OFFER_TO_BOOK"]
    await svc.runtime.stop()


async def test_cancel_declined_changes_nothing(g: FakeGoogle) -> None:
    svc = await _service(g, cancel_draft_enabled=False)
    code = await _book(svc)
    jobs = len(svc.runtime.store.jobs())
    sid, _ = await _say(svc, None, "yes", "cancel", code, "no")
    assert svc.last_turn.state == "intent_detect"
    assert _ids(svc) == ["CANCEL_ABORTED", "ANYTHING_ELSE"]
    _, reply = await _say(svc, sid, "no")
    assert svc.last_turn.state == "close"
    assert len(svc.runtime.store.jobs()) == jobs
    assert len(g.calendar.live_events()) == 1
    await svc.runtime.stop()


async def test_cancel_without_draft_when_disabled(g: FakeGoogle) -> None:
    svc = await _service(g, cancel_draft_enabled=False)
    code = await _book(svc)
    await _say(svc, None, "yes", f"cancel {code}", "yes")
    assert await svc.runtime.worker.drain() == 2  # delete hold + notes only
    assert len(g.gmail.drafts_created) == 1
    await svc.runtime.stop()


async def test_cancel_waitlist_entry(g: FakeGoogle) -> None:
    svc = await _service(g, _no_tuesday())
    _, reply = await _say(svc, None, "yes", "book", "topic sip", "time tuesday afternoon")
    code = CODE_RE.search(_text(reply)).group(0)
    await svc.runtime.worker.drain()
    assert len(g.calendar.live_events()) == 1
    sid, _ = await _say(svc, None, "yes", f"cancel {code}")
    assert _ids(svc) == ["CANCEL_READBACK_WAITLIST"]
    await _say(svc, sid, "yes")
    assert _ids(svc) == ["CANCELLED", "GOODBYE"]
    assert await svc.runtime.worker.drain() == 3
    assert g.calendar.live_events() == []  # the waitlist marker hold is released too
    assert g.docs.lines()[-1].endswith(f"| {code} | cancelled")
    await svc.runtime.stop()


# --- Phase 6: what to prepare / availability ---------------------------------------------


@pytest.mark.parametrize(
    ("topic", "label"),
    [("kyc", "KYC/Onboarding"), ("sip", "SIP/Mandates"), ("statements", "Statements/Tax Docs"),
     ("withdrawals", "Withdrawals & Timelines"), ("nominee", "Account Changes/Nominee")],
)  # fmt: skip
async def test_prepare_reads_static_guide(g: FakeGoogle, topic: str, label: str) -> None:
    from advisor_agent.domain.models import Topic
    from advisor_agent.orchestrator.content import prep_guides

    svc = await _service(g)
    sid, reply = await _say(svc, None, "yes", "prepare", topic)
    assert _ids(svc) == ["PREP_GUIDE", "OFFER_TO_BOOK"]
    assert label in _text(reply) and prep_guides()[Topic(label)] in _text(reply)
    _, reply = await _say(svc, sid, "yes")  # topic carried into the booking
    assert svc.last_turn.state == "collect_pref" and _ids(svc) == ["TOPIC_ACK", "ASK_PREF"]
    assert svc.runtime.store.jobs() == []
    await svc.runtime.stop()


async def test_availability_peeks_without_side_effects(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid, reply = await _say(svc, None, "yes", "availability time tuesday afternoon")
    text = _text(reply)
    assert _ids(svc) == ["AVAILABILITY_LIST", "OFFER_TO_BOOK"]
    assert text.startswith("Here's what's open (IST): Tuesday, 6 October 2026: ")
    assert "AM" not in text.split("OFFER")[0].split("\n")[0]  # afternoon only
    decisions = len(svc.runtime.runner.decisions)

    _, reply = await _say(svc, sid, "yes", "topic sip")  # preference remembered
    assert svc.last_turn.state == "offer_slots"
    assert all(s.startswith("Tuesday, 6 October 2026") for s in re.findall(FULL_SLOT, _text(reply)))
    assert svc.runtime.store.jobs() == [] and g.calendar.live_events() == []
    assert len(svc.runtime.runner.decisions) >= decisions  # only read-only busy checks
    await svc.runtime.stop()


async def test_availability_lists_next_days_when_no_day_given(g: FakeGoogle) -> None:
    svc = await _service(g)
    _, reply = await _say(svc, None, "yes", "availability")
    assert _text(reply).count("2026:") == 3
    await svc.runtime.stop()


# --- no PII prompts in any Phase 5/6 template ----------------------------------------------


def test_new_templates_never_ask_for_personal_details() -> None:
    from advisor_agent.orchestrator.templates import load_templates

    banned = re.compile(r"phone|e-?mail|account number|\bpan\b|aadhaar|address", re.I)
    new = [
        k
        for k in load_templates()
        if k.startswith(("ASK_CODE", "CODE_", "CANCEL", "PREP", "AVAILABILITY", "PIVOT", "ADVICE"))
    ]
    assert new
    for key in new:
        assert not banned.search(load_templates()[key]), key
