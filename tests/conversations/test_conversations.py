"""Conversation scripts (LLD 2.15), each run through ChatService AND the HTTP API."""

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from advisor_agent.channels.chat.api import create_app
from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import ChatService
from advisor_agent.domain.slots import generate_free_slots, make_slot
from advisor_agent.domain.timeutil import to_ist, to_utc
from tests.conftest import make_settings

SCRIPTS = sorted(Path(__file__).parent.glob("*.yaml"))

Send = Callable[[str | None], Awaitable[dict[str, Any]]]


def _service(script: dict[str, Any]) -> ChatService:
    now = to_utc(datetime.fromisoformat(script["clock"]))
    cal = script.get("calendar")
    if cal is None:
        slots = generate_free_slots(to_ist(now).date(), working_days=14, seed=42)
    else:
        slots = [make_slot(datetime.fromisoformat(s)) for s in cal]
    return build_chat_service(
        make_settings(nlu_engine=script.get("nlu", "stub")), slots=slots, clock=lambda: now
    )


def _check(turn: dict[str, Any], reply: dict[str, Any], where: str) -> None:
    text = "\n".join(reply["messages"])
    if "expect_state" in turn:
        assert reply["state"] == turn["expect_state"], f"{where}: {text}"
    for s in turn.get("expect_contains", []):
        assert s in text, f"{where}: {s!r} not in {text!r}"
    for s in turn.get("expect_not_contains", []):
        assert s not in text, f"{where}: {s!r} in {text!r}"
    for rx in turn.get("expect_regex", []):
        assert re.search(rx, text), f"{where}: /{rx}/ not in {text!r}"
    if "expect_done" in turn:
        assert reply["done"] is turn["expect_done"], where


async def _run(script: dict[str, Any], send: Send) -> None:
    for i, turn in enumerate(script["turns"]):
        reply = await send(turn["user"])
        _check(turn, reply, f"{script['name']} turn {i} ({turn['user']!r})")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
async def test_via_chat_service(path: Path) -> None:
    script = yaml.safe_load(path.read_text())
    service = _service(script)
    session: dict[str, str] = {}

    async def send(text: str | None) -> dict[str, Any]:
        if text is None:
            reply = await service.start()
            session["id"] = reply.session_id
        else:
            reply = await service.send(session["id"], text)
        return reply.model_dump(mode="json")

    await _run(script, send)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
async def test_via_http_api(path: Path) -> None:
    script = yaml.safe_load(path.read_text())
    client = TestClient(create_app(_service(script), make_settings()))
    session: dict[str, str] = {}

    async def send(text: str | None) -> dict[str, Any]:
        if text is None:
            res = client.post("/v1/sessions")
            assert res.status_code == 201
            session["id"] = res.json()["session_id"]
        else:
            res = client.post(f"/v1/sessions/{session['id']}/messages", json={"user_text": text})
            assert res.status_code == 200, res.text
        return res.json()

    await _run(script, send)
