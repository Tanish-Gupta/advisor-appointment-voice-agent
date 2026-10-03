"""Chat HTTP API with the Phase 4 runtime: lifespan starts the MCP client + outbox worker; the
secure link in the chat reply opens the contact form on the same app."""

import re
import time
from urllib.parse import urlsplit

import pytest

from advisor_agent.channels.chat.api import create_app
from advisor_agent.channels.chat.bootstrap import build_chat_service
from tests.conftest import default_slots, fixed_clock, make_settings
from tests.fake_google import FakeGoogle


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def client(g: FakeGoogle):  # type: ignore[no-untyped-def]
    pytest.importorskip("cryptography")
    from fastapi.testclient import TestClient

    settings = make_settings(outbox_poll_s=0.05)
    service = build_chat_service(
        settings, slots=default_slots(), clock=fixed_clock, tool_target=g.server()
    )
    with TestClient(create_app(service, settings)) as c:
        yield c


def _wait_done(client, code: str, timeout: float = 5.0) -> dict:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while True:
        body = client.get("/admin/outbox", params={"code": code}).json()
        if all(j["status"] == "done" for j in body["jobs"]) or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


def test_book_via_http_then_secure_form(client, g: FakeGoogle) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/healthz").json()["mcp"] is True
    sid = client.post("/v1/sessions").json()["session_id"]
    for text in ("yes", "book", "topic kyc", "time monday morning", "2"):
        client.post(f"/v1/sessions/{sid}/messages", json={"user_text": text})
    body = client.post(f"/v1/sessions/{sid}/messages", json={"user_text": "yes"}).json()
    assert body["state"] == "close"
    text = "\n".join(body["messages"])
    code = re.search(r"\bNL-[A-Z]\d{3}\b", text).group(0)
    url = re.search(r"https?://\S+", text).group(0)

    outbox = _wait_done(client, code)
    assert [j["status"] for j in outbox["jobs"]] == ["done"] * 3
    assert outbox["booking"]["status"] == "tentative"
    assert g.calendar.live_events()[0]["summary"].endswith(code)

    parts = urlsplit(url)
    page = client.get(f"{parts.path}?{parts.query}")
    assert page.status_code == 200 and f"Booking {code}" in page.text
    hidden = dict(re.findall(r'name="(t|csrf)" value="([^"]+)"', page.text))
    resp = client.post(
        parts.path,
        data={
            **hidden,
            "name": "Asha",
            "phone": "+919876543210",
            "email": "a@example.com",
            "consent": "on",
        },  # fmt: skip
    )
    assert resp.status_code == 200
    # contact details never reach Google or the chat
    assert "9876543210" not in str(g.docs.text) + str(g.calendar.events_by_id)


def test_admin_unknown_code_404(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/admin/outbox", params={"code": "NL-A222"}).status_code == 404


def _chat(client, *texts: str) -> dict:  # type: ignore[no-untyped-def]
    sid = client.post("/v1/sessions").json()["session_id"]
    body: dict = {}
    for text in texts:
        body = client.post(f"/v1/sessions/{sid}/messages", json={"user_text": text}).json()
    return body


def test_phase5_6_flows_via_http(client, g: FakeGoogle) -> None:  # type: ignore[no-untyped-def]
    """Chat-complete gate #1: every intent + advice refusal through the HTTP API."""
    body = _chat(client, "yes", "book", "topic sip", "time tuesday afternoon")
    assert body["state"] == "offer_slots"
    sid = body["session_id"]
    send = lambda t: client.post(f"/v1/sessions/{sid}/messages", json={"user_text": t}).json()  # noqa: E731
    advice = send("which fund gives the best returns?")
    assert advice["state"] == "await_pivot"
    assert "investor.sebi.gov.in" in " ".join(advice["messages"])
    assert send("yes")["state"] == "offer_slots"
    send("1")
    booked = send("yes")
    code = re.search(r"\bNL-[A-Z]\d{3}\b", "\n".join(booked["messages"])).group(0)
    _wait_done(client, code)

    moved = _chat(client, "yes", f"reschedule {code}", "time wednesday morning", "1", "yes")
    assert moved["state"] == "close" and "moved to" in moved["messages"][0]
    assert len(_wait_done(client, code)["jobs"]) == 7

    prep = _chat(client, "yes", "prepare", "topic kyc")
    assert "KYC/Onboarding" in prep["messages"][0] and prep["state"] == "intent_detect"
    avail = _chat(client, "yes", "availability time thursday")
    assert "Thursday, 8 October 2026" in avail["messages"][0]

    cancelled = _chat(client, "yes", "cancel", code, "yes")
    assert cancelled["state"] == "close" and "cancelled" in cancelled["messages"][0]
    outbox = _wait_done(client, code)
    assert outbox["booking"]["status"] == "cancelled" and len(outbox["jobs"]) == 10
    assert g.calendar.live_events() == [] and g.docs.lines()[-1].endswith("| cancelled")
