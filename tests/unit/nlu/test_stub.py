from datetime import date, datetime

import pytest

from advisor_agent.domain.models import Intent, Topic
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.schema import NLUContext, YesNo
from advisor_agent.nlu.stub import StubNLU

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)  # Friday


async def parse(text: str, state: str = "intent_detect"):
    return await StubNLU().parse(text, state, NLUContext(state=state, ref=REF))


@pytest.mark.parametrize("text", ["yes", "Y", "I understand", "i agree.", "Yes!"])
async def test_yes(text):
    assert (await parse(text, "disclaimer_ack")).yes_no is YesNo.YES


@pytest.mark.parametrize("text", ["no", "n", "neither", "None of them"])
async def test_no(text):
    assert (await parse(text, "offer_slots")).yes_no is YesNo.NO


@pytest.mark.parametrize(
    ("text", "meta"), [("repeat", "repeat"), ("say again", "repeat"), ("stop", "stop"),
                       ("bye", "stop"), ("help", "help")],
)  # fmt: skip
async def test_meta(text, meta):
    assert (await parse(text)).meta == meta


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("book", Intent.BOOK_NEW),
        ("reschedule NL-A742", Intent.RESCHEDULE),
        ("cancel NL-A742", Intent.CANCEL),
        ("prepare", Intent.WHAT_TO_PREPARE),
        ("what to prepare", Intent.WHAT_TO_PREPARE),
        ("availability", Intent.CHECK_AVAILABILITY),
        ("check availability", Intent.CHECK_AVAILABILITY),
    ],
)
async def test_intents(text, intent):
    r = await parse(text)
    assert r.intent is intent and r.confidence == 1.0


async def test_code_extracted_for_reschedule_and_cancel():
    assert (await parse("reschedule NL-A742")).booking_code == "NL-A742"
    assert (await parse("cancel nl a742")).booking_code == "NL-A742"


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("topic kyc", Topic.KYC_ONBOARDING),
        ("topic sip", Topic.SIP_MANDATES),
        ("topic statements", Topic.STATEMENTS_TAX),
        ("topic withdrawals", Topic.WITHDRAWALS),
        ("topic nominee", Topic.ACCOUNT_CHANGES),
    ],
)
async def test_topic_commands(text, topic):
    assert (await parse(text)).topic is topic


@pytest.mark.parametrize("topic", list(Topic))
async def test_quick_reply_labels_parse_as_topics(topic):
    assert (await parse(topic.value, "topic_confirm")).topic is topic


@pytest.mark.parametrize(("digit", "topic"), list(enumerate(Topic, start=1)))
async def test_topic_numbers_in_topic_state(digit, topic):
    assert (await parse(str(digit), "topic_confirm")).topic is topic


@pytest.mark.parametrize(
    ("text", "d", "part"),
    [
        ("time tuesday afternoon", date(2026, 10, 6), "afternoon"),
        ("time tue morning", date(2026, 10, 6), "morning"),
        ("time friday evening", date(2026, 10, 2), "evening"),  # today counts
        ("time thursday", date(2026, 10, 8), None),
        ("time 2026-10-07 morning", date(2026, 10, 7), "morning"),
        ("time tomorrow", date(2026, 10, 3), None),
        ("time today afternoon", date(2026, 10, 2), "afternoon"),
        ("time afternoon", None, "afternoon"),
    ],
)
async def test_time_commands(text, d, part):
    r = await parse(text)
    assert r.date_iso == d and r.time_text == part


async def test_bare_time_in_collect_pref():
    r = await parse("Monday morning", "collect_pref")
    assert r.date_iso == date(2026, 10, 5) and r.time_text == "morning"


async def test_any_is_a_preference():
    r = await parse("any", "collect_pref")
    assert r.date_text == "any" and r.date_iso is None


@pytest.mark.parametrize(
    ("text", "choice"),
    [("1", 1), ("first", 1), ("Option 1", 1), ("the first one", 1), ("2", 2), ("second slot", 2)],
)
async def test_slot_choice(text, choice):
    assert (await parse(text, "offer_slots")).slot_choice == choice


async def test_compound_book_command():
    r = await parse("book topic kyc time monday morning")
    assert r.intent is Intent.BOOK_NEW
    assert r.topic is Topic.KYC_ONBOARDING
    assert r.date_iso == date(2026, 10, 5) and r.time_text == "morning"


@pytest.mark.parametrize("text", ["hello there", "what's the weather", "1"])
async def test_unknown(text):
    r = await parse(text)
    assert r.intent is Intent.UNKNOWN and r.confidence == 0.0
