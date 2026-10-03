"""Transition-table tests (LLD 2.8, Phase 2 rows), driven through ChatService."""

from datetime import datetime

import pytest

from advisor_agent.channels.chat.service import ChatService, InvalidMessage, SessionNotFound
from advisor_agent.domain.slots import make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.orchestrator.states import State

FULL_SLOT = r"\w+day, \d{1,2} \w+ \d{4}, \d{1,2}:\d{2} (AM|PM) IST"


async def walk(service: ChatService, *texts: str):
    reply = await service.start()
    for t in texts:
        reply = await service.send(reply.session_id, t)
    return reply


def templates(service: ChatService) -> list[str]:
    assert service.last_turn is not None
    return service.last_turn.template_ids


# --- disclaimer --------------------------------------------------------------------------


async def test_start_emits_greet_disclaimer_ack(service):
    reply = await service.start()
    assert reply.state is State.DISCLAIMER_ACK
    assert templates(service) == ["GREET", "DISCLAIMER", "DISCLAIMER_ASK_ACK"]
    assert any("not investment advice" in m for m in reply.messages)
    assert reply.quick_replies == ["yes", "no"]


@pytest.mark.parametrize("ack", ["yes", "I understand", "i agree"])
async def test_ack_moves_to_intent_detect(service, ack):
    reply = await walk(service, ack)
    assert reply.state is State.INTENT_DETECT
    assert templates(service) == ["ACK_THANKS", "ASK_HOW_HELP"]


async def test_one_refusal_repeats_disclaimer(service):
    reply = await walk(service, "no")
    assert reply.state is State.DISCLAIMER_ACK
    assert templates(service) == ["DISCLAIMER", "DISCLAIMER_ASK_ACK"]


@pytest.mark.parametrize("answers", [("no", "no"), ("hello", "book"), ("no", "maybe")])
async def test_two_refusals_close(service, answers):
    reply = await walk(service, *answers)
    assert reply.state is State.CLOSE and reply.done
    assert templates(service) == ["CANNOT_PROCEED_WITHOUT_ACK", "GOODBYE"]


# --- intent / clarify --------------------------------------------------------------------


async def test_book_asks_topic(service):
    reply = await walk(service, "yes", "book")
    assert reply.state is State.TOPIC_CONFIRM
    assert templates(service) == ["ASK_TOPIC"]
    assert len(reply.quick_replies) == 5


async def test_unknown_goes_to_clarify_with_stub_help(service):
    reply = await walk(service, "yes", "hello there")
    assert reply.state is State.CLARIFY
    assert templates(service) == ["CLARIFY_INTENT", "STUB_HELP"]


async def test_clarify_then_book(service):
    reply = await walk(service, "yes", "hmm", "book")
    assert reply.state is State.TOPIC_CONFIRM


async def test_three_clarify_failures_close_with_fallback(service):
    reply = await walk(service, "yes", "hmm", "what", "huh")
    assert reply.state is State.CLOSE
    assert templates(service) == ["FALLBACK_SECURE_LINK", "GOODBYE"]


@pytest.mark.parametrize(
    ("text", "state", "ids"),
    [
        # This fixture has no MCP executor: code lookups are reported as not enabled.
        ("reschedule NL-A742", State.INTENT_DETECT, ["CODE_LOOKUP_NOT_ENABLED", "ANYTHING_ELSE"]),
        ("cancel NL-A742", State.INTENT_DETECT, ["CODE_LOOKUP_NOT_ENABLED", "ANYTHING_ELSE"]),
        ("prepare", State.PREP_TOPIC, ["PREP_ASK_TOPIC"]),
        ("prepare topic kyc", State.INTENT_DETECT, ["PREP_GUIDE", "OFFER_TO_BOOK"]),
        ("availability", State.INTENT_DETECT, ["AVAILABILITY_LIST", "OFFER_TO_BOOK"]),
    ],
)
async def test_secondary_intents_are_routed(service, text, state, ids):
    reply = await walk(service, "yes", text)
    assert reply.state is state
    assert templates(service) == ids


async def test_book_with_topic_skips_to_pref(service):
    reply = await walk(service, "yes", "book topic kyc")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["TOPIC_ACK", "ASK_PREF"]
    assert "KYC/Onboarding" in reply.messages[0]


async def test_book_with_topic_and_time_skips_to_offer(service):
    reply = await walk(service, "yes", "book topic kyc time tuesday afternoon")
    assert reply.state is State.OFFER_SLOTS
    assert templates(service) == ["TOPIC_ACK", "OFFER_TWO"]


async def test_time_before_topic_is_remembered(service):
    reply = await walk(service, "yes", "book time tuesday afternoon", "topic sip")
    assert reply.state is State.OFFER_SLOTS
    assert "Tuesday, 6 October 2026" in reply.messages[-1]


async def test_bare_topic_implies_book(service):
    reply = await walk(service, "yes", "topic sip")
    assert reply.state is State.COLLECT_PREF


# --- topic / preference ------------------------------------------------------------------


async def test_topic_no_match_lists_options(service):
    reply = await walk(service, "yes", "book", "mortgages")
    assert reply.state is State.TOPIC_CONFIRM
    assert templates(service) == ["TOPIC_OPTIONS"]


@pytest.mark.parametrize("label", ["SIP/Mandates", "2", "sip"])
async def test_topic_match(service, label):
    reply = await walk(service, "yes", "book", label)
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["TOPIC_ACK", "ASK_PREF"]


async def test_pref_unclear(service):
    reply = await walk(service, "yes", "book", "topic sip", "whenever you like, mate")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["PREF_UNCLEAR"]


async def test_pref_weekend(service):
    reply = await walk(service, "yes", "book", "topic sip", "time saturday morning")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["PREF_NON_WORKING_DAY"]


async def test_pref_out_of_range(service):
    reply = await walk(service, "yes", "book", "topic sip", "time 2026-12-01 morning")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["PREF_OUT_OF_RANGE"]


async def test_empty_calendar_asks_for_other_time(make_service):
    service = make_service(slots=[])
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["NO_MATCH_TRY_OTHER"]
    assert "Tuesday, 6 October 2026 (afternoon)" in reply.messages[0]


# --- offer slots -------------------------------------------------------------------------


async def test_offer_two_full_ist_strings(service):
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    assert reply.state is State.OFFER_SLOTS
    assert reply.messages[0].count("Tuesday, 6 October 2026") == 2
    assert reply.messages[0].count("IST") >= 3
    assert reply.quick_replies == ["Option 1", "Option 2", "neither"]


async def test_single_slot_offer_and_yes(make_service):
    only = make_slot(datetime(2026, 10, 6, 14, 0, tzinfo=IST))
    service = make_service(slots=[only])
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    assert templates(service) == ["OFFER_ONE"]
    assert reply.quick_replies == ["yes", "neither"]
    reply = await service.send(reply.session_id, "yes")
    assert reply.state is State.CONFIRM_SLOT
    assert "Tuesday, 6 October 2026, 2:00 PM IST" in reply.messages[0]


@pytest.mark.parametrize(("choice", "idx"), [("1", 0), ("second", 1), ("Option 2", 1)])
async def test_choose_slot_reads_back(service, choice, idx):
    offer = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    reply = await service.send(offer.session_id, choice)
    assert reply.state is State.CONFIRM_SLOT
    assert templates(service) == ["CONFIRM_READBACK"]
    slot_text = offer.messages[0].split(") ")[idx + 1].split(" IST")[0]
    assert slot_text in reply.messages[0]
    assert "**SIP/Mandates**" in reply.messages[0]


async def test_neither_reoffers_different_slots(service):
    first = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    second = await service.send(first.session_id, "neither")
    assert second.state is State.OFFER_SLOTS
    assert second.messages != first.messages


async def test_neither_until_exhausted_asks_other_pref(make_service):
    slots = [make_slot(datetime(2026, 10, 6, h, 0, tzinfo=IST)) for h in (13, 14)]
    service = make_service(slots=slots)
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon", "no")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["ASK_OTHER_PREF"]


async def test_new_time_while_offering_searches_again(service):
    reply = await walk(
        service, "yes", "book", "topic sip", "time tuesday afternoon", "wednesday morning"
    )
    assert reply.state is State.OFFER_SLOTS
    assert "Wednesday, 7 October 2026" in reply.messages[0]


async def test_offer_reprompt_on_gibberish(service):
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon", "blue")
    assert reply.state is State.OFFER_SLOTS
    assert templates(service) == ["OFFER_REPROMPT"]


# --- confirm -----------------------------------------------------------------------------


async def test_confirm_no_reoffers_same_slots(service):
    offer = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    await service.send(offer.session_id, "1")
    reply = await service.send(offer.session_id, "no")
    assert reply.state is State.OFFER_SLOTS
    assert reply.messages == offer.messages


async def test_confirm_yes_without_mcp_tools_is_not_enabled(service):
    # AGENT_MCP_TOOLS_ENABLED=false (the test default): no executor, nothing is booked.
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon", "1", "yes")
    assert reply.state is State.CONFIRM_SLOT
    assert reply.messages == ["Booking isn't switched on in this build yet."]
    assert reply.booking_code is None


async def test_confirm_reprompt(service):
    reply = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon", "1", "hmm")
    assert reply.state is State.CONFIRM_SLOT
    assert templates(service) == ["CONFIRM_REPROMPT"]


# --- meta, close, errors -----------------------------------------------------------------


async def test_repeat_returns_last_messages(service):
    offer = await walk(service, "yes", "book", "topic sip", "time tuesday afternoon")
    again = await service.send(offer.session_id, "repeat")
    assert again.messages == offer.messages and again.state is State.OFFER_SLOTS


async def test_stop_closes_and_session_ends(service):
    reply = await walk(service, "yes", "stop")
    assert reply.state is State.CLOSE and reply.done
    after = await service.send(reply.session_id, "book")
    assert after.state is State.CLOSE
    assert templates(service) == ["SESSION_ENDED"]


async def test_stop_before_ack_is_allowed(service):
    reply = await walk(service, "stop")
    assert reply.state is State.CLOSE


async def test_handler_exception_gives_safe_fallback(service, monkeypatch):
    offer = await walk(service, "yes", "book", "topic sip")
    from advisor_agent.domain.slots import SlotPicker

    def boom(*a, **k):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(SlotPicker, "pick_two", boom)
    reply = await service.send(offer.session_id, "time tuesday afternoon")
    assert reply.state is State.COLLECT_PREF
    assert templates(service) == ["SAFE_FALLBACK", "ASK_PREF"]


async def test_unknown_session(service):
    with pytest.raises(SessionNotFound):
        await service.send("nope", "hi")


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
async def test_invalid_message(service, text):
    reply = await service.start()
    with pytest.raises(InvalidMessage):
        await service.send(reply.session_id, text)
