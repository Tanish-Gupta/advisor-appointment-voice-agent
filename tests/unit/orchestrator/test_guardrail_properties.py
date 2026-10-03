"""Guardrail properties: the disclaimer gate (Hypothesis) and the PII gate at every state."""

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.nlu.stub import _normalise
from advisor_agent.orchestrator.states import State
from tests.conftest import default_slots, fixed_clock, make_settings

ACK = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "yes please", "i understand", "i agree"}
POOL = ["no", "book", "topic sip", "time tuesday afternoon", "1", "2", "neither", "repeat",
        "help", "reschedule NL-A742", "hello", "cancel", "my email is a@b.co"]  # fmt: skip


def _not_ack(s: str) -> bool:
    return s.strip() != "" and _normalise(s) not in ACK


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.one_of(st.sampled_from(POOL), st.text(min_size=1, max_size=40).filter(_not_ack)),
        min_size=1,
        max_size=8,
    )
)
def test_never_past_disclaimer_without_ack(inputs):
    async def run():
        service = build_chat_service(make_settings(), slots=default_slots(), clock=fixed_clock)
        reply = await service.start()
        for text in inputs:
            if not text.strip():
                continue
            reply = await service.send(reply.session_id, text)
            assert reply.state in {State.DISCLAIMER_ACK, State.CLOSE}
            if reply.done:
                break

    asyncio.run(run())


PII_INPUTS = ["call me on 9876543210", "jane.doe@example.com", "my PAN is ABCDE1234F"]
PATHS = {
    State.DISCLAIMER_ACK: [],
    State.INTENT_DETECT: ["yes"],
    State.TOPIC_CONFIRM: ["yes", "book"],
    State.COLLECT_PREF: ["yes", "book", "topic sip"],
    State.OFFER_SLOTS: ["yes", "book", "topic sip", "time tuesday afternoon"],
    State.CONFIRM_SLOT: ["yes", "book", "topic sip", "time tuesday afternoon", "1"],
}


@pytest.mark.parametrize("state", list(PATHS))
@pytest.mark.parametrize("pii_text", PII_INPUTS)
async def test_pii_deflected_at_every_state(make_service, audit, state, pii_text):
    service = make_service()
    reply = await service.start()
    for t in PATHS[state]:
        reply = await service.send(reply.session_id, t)
    assert reply.state is state
    before = reply.messages

    deflected = await service.send(reply.session_id, pii_text)
    assert deflected.state is state
    assert deflected.messages[0].startswith("For your safety")
    assert service.last_turn.template_ids[0] == "PII_DEFLECT"
    assert len(deflected.messages) == 2  # deflect + re-prompt of the current question
    if state in {State.OFFER_SLOTS, State.CONFIRM_SLOT}:
        assert deflected.messages[1] == before[-1]

    turns = [e for e in audit.events if e.kind == "turn"]
    assert "[REDACTED:" in turns[-1].data["text"]
    for e in audit.events:
        for v in e.data.values():
            assert "9876543210" not in str(v)
            assert "jane.doe@example.com" not in str(v)
            assert "ABCDE1234F" not in str(v)
