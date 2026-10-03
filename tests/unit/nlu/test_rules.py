"""RulesNLU (LLD 3.5). Reference: Friday 2 Oct 2026, 16:00 IST."""

from datetime import date, datetime

import pytest

from advisor_agent.domain.models import Intent, Topic
from advisor_agent.domain.slots import make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.rules import RULE_CONFIDENCE, RulesNLU, detect_intent, normalise
from advisor_agent.nlu.schema import NLUContext, YesNo

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
OFFERED = [
    make_slot(datetime(2026, 10, 6, 14, 0, tzinfo=IST)),  # Tuesday 2:00 PM
    make_slot(datetime(2026, 10, 7, 15, 0, tzinfo=IST)),  # Wednesday 3:00 PM
]
NLU = RulesNLU()


def parse(text: str, state: str = "intent_detect", offered=None):
    return NLU.parse_sync(text, state, offered or [], REF)


async def test_async_parse_uses_context():
    ctx = NLUContext(state="offer_slots", offered_slots=OFFERED, ref=REF)
    result = await NLU.parse("the second one", "offer_slots", ctx)
    assert result.slot_choice == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.30 P.M.", "3:30 pm"), ("Hello!! Book—now", "hello book now"),
     ("e-kyc - done", "e-kyc done"), ("  I’m   FREE ", "i'm free")],
)  # fmt: skip
def test_normalise(text, expected):
    assert normalise(text) == expected


@pytest.mark.parametrize(
    ("text", "meta"),
    [("repeat", "repeat"), ("Repeat that, please.", "repeat"), ("say that again", "repeat"),
     ("pardon?", "repeat"), ("help", "help"), ("what can you do", "help"), ("bye", "stop"),
     ("Goodbye!", "stop"), ("that's all", "stop")],
)  # fmt: skip
def test_meta(text, meta):
    result = parse(text)
    assert result.meta == meta
    assert result.confidence == RULE_CONFIDENCE


@pytest.mark.parametrize("text", ["hi", "Hello there!", "thanks", "how are you?", "good morning"])
def test_small_talk(text):
    result = parse(text)
    assert result.intent is Intent.SMALL_TALK
    assert result.time_text is None  # "good morning" is a greeting, not a time


def test_greeting_prefix_is_not_a_time():
    result = parse("Good morning, I want to book a slot")
    assert result.intent is Intent.BOOK_NEW
    assert result.time_text is None


def test_greeting_prefix_needs_word_boundary():
    # "hi" must not be stripped from "highest"
    assert parse("highest returns please").intent is Intent.INVESTMENT_ADVICE


@pytest.mark.parametrize(
    ("text", "state", "expected"),
    [
        ("yes", "disclaimer_ack", YesNo.YES),
        ("I understand", "disclaimer_ack", YesNo.YES),
        ("sure, go ahead", "confirm_slot", YesNo.YES),
        ("no problem", "confirm_slot", YesNo.YES),
        ("yep that works", "confirm_slot", YesNo.YES),
        ("no", "confirm_slot", YesNo.NO),
        ("nope", "disclaimer_ack", YesNo.NO),
        ("neither", "offer_slots", YesNo.NO),
        ("those don't work for me", "offer_slots", YesNo.NO),
        ("yes but none of these", "offer_slots", YesNo.UNCLEAR),
    ],
)
def test_yes_no(text, state, expected):
    assert parse(text, state, OFFERED).yes_no is expected


def test_yes_no_only_in_yes_no_states():
    assert parse("yes", "collect_pref").yes_no is None


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I want to book an appointment", Intent.BOOK_NEW),
        ("Can I speak to an advisor?", Intent.BOOK_NEW),
        ("book an available slot tomorrow", Intent.BOOK_NEW),  # explicit book beats availability
        ("I need to move my slot", Intent.RESCHEDULE),
        ("please reschedule my appointment", Intent.RESCHEDULE),
        ("please cancel my booking", Intent.CANCEL),
        ("cancel my appointment about my SIP", Intent.CANCEL),
        ("what documents do I need?", Intent.WHAT_TO_PREPARE),
        ("What should I prepare for the KYC call?", Intent.WHAT_TO_PREPARE),
        ("what slots are available on Tuesday?", Intent.CHECK_AVAILABILITY),
        ("which fund should I buy?", Intent.INVESTMENT_ADVICE),
        ("Should I sell my SIP?", Intent.INVESTMENT_ADVICE),
        ("what are the best mutual funds", Intent.INVESTMENT_ADVICE),
        ("will the market crash", Intent.INVESTMENT_ADVICE),
    ],
)
def test_intents(text, intent):
    assert parse(text).intent is intent


def test_cancel_a_topic_object_is_not_cancel():
    result = parse("cancel my SIP")
    assert result.intent is None  # "cancel my SIP" is a SIP/Mandates question, not a booking
    assert result.topic is Topic.SIP_MANDATES
    assert result.confidence == RULE_CONFIDENCE


def test_advice_clears_topic():
    result = parse("Should I sell my SIP?")
    assert result.topic is None and result.topic_candidates == []


def test_tax_returns_is_a_topic_not_advice():
    result = parse("I need help with my tax returns")
    assert result.intent is None
    assert result.topic is Topic.STATEMENTS_TAX


def test_detect_intent_none():
    assert detect_intent("the weather is nice") is None


def test_unknown_has_low_confidence():
    result = parse("the weather is nice")
    assert result.intent is Intent.UNKNOWN
    assert result.confidence < 0.5


@pytest.mark.parametrize(
    ("text", "topic"),
    [("2", Topic.SIP_MANDATES), ("the first one", Topic.KYC_ONBOARDING),
     ("option 5", Topic.ACCOUNT_CHANGES), ("nominee update", Topic.ACCOUNT_CHANGES)],
)  # fmt: skip
def test_topic_state_picks(text, topic):
    assert parse(text, "topic_confirm").topic is topic


def test_numbers_are_not_topics_outside_topic_states():
    assert parse("2", "intent_detect").topic is None


def test_ambiguous_topic():
    result = parse("tax on my SIP withdrawal", "topic_confirm")
    assert result.topic is None
    assert result.topic_candidates == [
        Topic.SIP_MANDATES, Topic.STATEMENTS_TAX, Topic.WITHDRAWALS
    ]


@pytest.mark.parametrize(
    ("text", "date_text", "date_iso", "time_text"),
    [
        ("next tuesday after 3 pm", "next tuesday", date(2026, 10, 6), "after 15:00"),
        ("tomorrow morning", "tomorrow", date(2026, 10, 3), "morning"),
        ("Wednesday around 11", "wednesday", date(2026, 10, 7), "around 11:00"),
        ("anytime", "any", None, None),
        ("any day in the afternoon", "any", None, "afternoon"),
    ],
)
def test_preferences(text, date_text, date_iso, time_text):
    result = parse(text, "collect_pref")
    assert (result.date_text, result.date_iso, result.time_text) == (date_text, date_iso, time_text)
    assert result.booking_code is None


@pytest.mark.parametrize(
    ("text", "choice"),
    [
        ("the second one", 2),
        ("option 1", 1),
        ("2", 2),
        ("first", 1),
        ("I'll take the first", 1),
        ("the latter", 2),
        ("the last one", 2),
        ("the 2nd", 2),
        ("the 3 pm one", 2),
        ("the tuesday one", 1),
        ("wednesday at 3", 2),
    ],
)
def test_slot_choice(text, choice):
    assert parse(text, "offer_slots", OFFERED).slot_choice == choice


def test_the_2nd_is_not_a_date_when_choosing():
    assert parse("the 2nd", "offer_slots", OFFERED).date_text is None


@pytest.mark.parametrize("text", ["tuesday", "tuesday afternoon", "first thing tomorrow",
                                  "neither", "the 5 pm one"])  # fmt: skip
def test_no_slot_choice(text):
    assert parse(text, "offer_slots", OFFERED).slot_choice is None


def test_new_preference_while_offering():
    result = parse("no, thursday morning instead", "offer_slots", OFFERED)
    assert result.yes_no is YesNo.NO
    assert result.slot_choice is None
    assert (result.date_iso, result.time_text) == (date(2026, 10, 8), "morning")


@pytest.mark.parametrize(
    ("text", "state"),
    [("reschedule my appointment NL-A742", "intent_detect"), ("my code is NL-A742", "clarify"),
     ("NL-A742", "ask_code"), ("cancel nl a742", "intent_detect")],
)  # fmt: skip
def test_booking_code(text, state):
    assert parse(text, state).booking_code == "NL-A742"
