"""End to end through the chat service: confirm -> EXECUTE -> Gemini (faked LLM) proposes MCP
calls -> ToolGate -> real FastMCP server -> Google wrappers (fake HTTP layer) via the outbox.

Only Google's HTTP endpoint and the Gemini model are faked; MCP, the gate, the outbox, the
store, and the secure link are the real implementations.
"""

import json
import re
from typing import Any
from urllib.parse import urlsplit

import pytest

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import ChatService
from advisor_agent.domain.models import BookingStatus
from tests.conftest import default_slots, fixed_clock, make_settings
from tests.fake_google import FakeGoogle

URL_RE = re.compile(r"https?://\S+")
CODE_RE = re.compile(r"\bNL-[A-Z]\d{3}\b")


def faithful_llm(log: list[list[str]]):  # type: ignore[no-untyped-def]
    """A well-behaved model: maps FACTS to one call per allowed tool."""

    async def generate(system: str, facts_json: str, decls: list, allowed: list[str]):  # type: ignore[no-untyped-def]
        assert {d["name"] for d in decls} <= set(allowed)
        assert "Never include personal data" in system
        f = json.loads(facts_json)["FACTS"]
        log.append(list(allowed))
        calls = {
            "calendar_list_busy": {"start_ist": f["slot_start_ist"], "end_ist": f["slot_end_ist"]},
            "calendar_create_hold": {
                "code": f["booking_code"], "topic": f["topic"], "start_ist": f["slot_start_ist"],
                "end_ist": f["slot_end_ist"], "kind": f["kind"], "version": float(f["version"]),
            },
            "docs_append_prebooking": {
                "date": f["booked_on"], "topic": f["topic"], "slot": f["slot_label"],
                "code": f["booking_code"], "status": f["notes_status"],
            },
            "gmail_create_draft": {
                "template": f["email_template"], "code": f["booking_code"], "topic": f["topic"],
                "slot": f["slot_label"],
            },
        }  # fmt: skip
        return [(t, calls[t]) for t in allowed]

    return generate


def hallucinating_llm():  # type: ignore[no-untyped-def]
    async def generate(system, facts_json, decls, allowed):  # type: ignore[no-untyped-def]
        return [("gmail_create_draft", {"template": "booking", "code": "NL-Z999", "topic": "x",
                                        "slot": "call +91 98765 43210"})]  # fmt: skip

    return generate


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


async def _service(g: FakeGoogle, generate: Any = None, **settings: Any) -> ChatService:
    svc = build_chat_service(
        make_settings(**settings),
        slots=default_slots(),
        clock=fixed_clock,
        tool_target=g.server(),
        tool_generate=generate,
    )
    assert svc.runtime is not None
    await svc.runtime.start(run_worker=False)
    return svc


async def _to_confirm(svc: ChatService) -> str:
    reply = await svc.start()
    sid = reply.session_id
    for text in ("yes", "book", "topic sip", "time tuesday afternoon", "1"):
        reply = await svc.send(sid, text)
    assert svc.last_turn.state == "confirm_slot"
    return sid


async def test_book_new_end_to_end(g: FakeGoogle) -> None:
    calls: list[list[str]] = []
    svc = await _service(g, faithful_llm(calls))
    sid = await _to_confirm(svc)
    reply = await svc.send(sid, "yes")
    text = "\n".join(reply.messages)

    assert svc.last_turn.state == "close" and reply.done
    code = CODE_RE.search(text).group(0)
    url = URL_RE.search(text).group(0)
    assert re.search(r"Tuesday, 6 October 2026, \d{1,2}:\d{2} (AM|PM) IST", text)
    assert urlsplit(url).path == f"/b/{code}" and "valid for 48 hours" in text
    assert calls and calls[-1] == [
        "calendar_list_busy", "calendar_create_hold", "docs_append_prebooking",
        "gmail_create_draft",
    ]  # fmt: skip
    booking_decisions = list(svc.runtime.runner.decisions)[-4:]
    assert [d["source"] for d in booking_decisions] == ["llm"] * 4
    assert all(d["decision"] == "approved" for d in booking_decisions)

    # nothing hits Google writes until the outbox runs
    assert g.calendar.live_events() == [] and g.gmail.drafts_created == []
    assert await svc.runtime.worker.drain() == 3

    (event,) = g.calendar.live_events()
    assert event["summary"] == f"Advisor Q&A \u2014 SIP/Mandates \u2014 {code}"
    assert event["status"] == "tentative"
    (line,) = g.docs.lines()
    assert line.startswith("2026-10-02 | SIP/Mandates | ") and line.endswith(
        f"| {code} | tentative"
    )
    (draft,) = g.gmail.drafts_created
    assert draft["To"] == "advisor@example.com" and code in str(draft["Subject"])
    booking = svc.runtime.store.get_booking(code)
    assert booking.status is BookingStatus.TENTATIVE and booking.calendar_event_id == event["id"]
    await svc.runtime.stop()


async def test_hallucinated_llm_calls_fall_back_to_plan(g: FakeGoogle) -> None:
    svc = await _service(g, hallucinating_llm())
    sid = await _to_confirm(svc)
    reply = await svc.send(sid, "yes")
    code = CODE_RE.search("\n".join(reply.messages)).group(0)
    labels = [(d["source"], d["decision"]) for d in svc.runtime.runner.decisions]
    assert ("llm", "rejected:not_allowed_in_step") not in labels  # gmail is allowed in EXECUTE
    assert any(src == "llm" and dec.startswith("rejected") for src, dec in labels)
    assert labels[-1][0] == "fallback"
    await svc.runtime.worker.drain()
    assert g.calendar.live_events()[0]["summary"].endswith(code)
    assert "NL-Z999" not in str(g.gmail.drafts_created[0]["Subject"])
    await svc.runtime.stop()


async def test_without_llm_plan_is_used(g: FakeGoogle) -> None:
    svc = await _service(g, None, mcp_llm_tool_calling=False)
    sid = await _to_confirm(svc)
    await svc.send(sid, "yes")
    assert svc.last_turn.state == "close"
    assert await svc.runtime.worker.drain() == 3
    await svc.runtime.stop()


async def test_slot_taken_on_calendar_reoffers(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid = await _to_confirm(svc)
    # someone booked the advisor's calendar directly in the meantime
    slot = svc.snapshot(sid)["chosen_slot"]
    g.calendar.external_busy = [{"start": slot["start_utc"], "end": slot["end_utc"]}]
    reply = await svc.send(sid, "yes")
    assert "just taken" in reply.messages[0]
    assert svc.last_turn.state in ("offer_slots", "collect_pref")
    assert svc.runtime.store.jobs() == []
    await svc.runtime.stop()


async def test_busy_slots_are_not_offered(g: FakeGoogle) -> None:
    svc = await _service(g)
    reply = await svc.start()
    for text in ("yes", "book", "topic sip"):
        reply = await svc.send(reply.session_id, text)
    # the advisor blocked Tuesday afternoon directly in Google Calendar
    g.calendar.external_busy = [
        {"start": "2026-10-06T12:00:00+05:30", "end": "2026-10-06T18:00:00+05:30"}
    ]
    reply = await svc.send(reply.session_id, "time tuesday afternoon")
    assert "Tuesday, 6 October 2026, 1" not in "\n".join(reply.messages)
    assert "Tuesday, 6 October 2026, 2" not in "\n".join(reply.messages)
    await svc.runtime.stop()


async def test_mcp_outage_at_confirm_asks_to_retry(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid = await _to_confirm(svc)

    def broken(*_: Any) -> Any:
        raise RuntimeError("database is down")

    svc.runtime.executor._bookings.create = broken  # type: ignore[method-assign]
    reply = await svc.send(sid, "yes")
    assert svc.last_turn.state == "confirm_slot"
    assert "try again" in reply.messages[0]
    await svc.runtime.stop()


async def test_second_yes_never_double_books(g: FakeGoogle) -> None:
    svc = await _service(g)
    sid = await _to_confirm(svc)
    r1 = await svc.runtime.executor.book(sid, *_topic_slot(svc, sid))
    r2 = await svc.runtime.executor.book(sid, *_topic_slot(svc, sid))
    assert r1.code == r2.code and r1.secure_url != r2.secure_url
    assert len(svc.runtime.store.jobs()) == 3
    await svc.runtime.stop()


def _topic_slot(svc: ChatService, sid: str):  # type: ignore[no-untyped-def]
    from advisor_agent.domain.models import Slot, Topic

    ctx = svc.snapshot(sid)
    return Topic(ctx["topic"]), Slot.model_validate(ctx["chosen_slot"])
