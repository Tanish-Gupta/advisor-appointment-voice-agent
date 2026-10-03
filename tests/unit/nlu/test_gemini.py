"""GeminiNLU (LLD 3.3 / 3.6) with a fake GenerateFn: prompt, sanitising, failure modes.

No network: the Google SDK is never imported here.
"""

import asyncio
import json
from datetime import date, datetime
from typing import Any

import pytest

from advisor_agent.domain.models import Intent, Topic
from advisor_agent.domain.slots import make_slot
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.gemini import (
    RESPONSE_SCHEMA,
    GeminiExtraction,
    GeminiNLU,
    NLUUnavailable,
    build_system_prompt,
)
from advisor_agent.nlu.schema import NLUContext, YesNo

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
OFFERED = [
    make_slot(datetime(2026, 10, 6, 14, 0, tzinfo=IST)),
    make_slot(datetime(2026, 10, 7, 15, 0, tzinfo=IST)),
]


class FakeGenerate:
    def __init__(self, reply: Any = None, *, exc: Exception | None = None, delay: float = 0.0):
        self.reply, self.exc, self.delay = reply, exc, delay
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user_text: str) -> str:
        self.calls.append((system, user_text))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def ctx(state: str = "collect_pref", offered=None, flow: str | None = "book_new") -> NLUContext:
    return NLUContext(state=state, flow=flow, offered_slots=offered or [], ref=REF)


async def run(reply: Any, state: str = "collect_pref", offered=None, text: str = "hi"):
    return await GeminiNLU(FakeGenerate(reply)).parse(text, state, ctx(state, offered))


def test_schema_lists_every_intent_and_topic():
    props = RESPONSE_SCHEMA["properties"]
    assert set(props["intent"]["enum"]) == {i.value for i in Intent}
    assert set(props["topic"]["enum"]) == {t.value for t in Topic}
    assert RESPONSE_SCHEMA["required"] == ["intent", "confidence"]


def test_system_prompt_has_context_and_no_placeholders():
    prompt = build_system_prompt(ctx("offer_slots", OFFERED))
    assert "Friday, 2 October 2026 (2026-10-02)" in prompt
    assert "4:00 PM IST" in prompt
    assert "offer_slots" in prompt and "book_new" in prompt
    assert "which of the offered slots they want" in prompt
    assert "1) Tuesday, 6 October 2026, 2:00 PM IST" in prompt
    assert "2) Wednesday, 7 October 2026, 3:00 PM IST" in prompt
    assert "{" not in prompt and "}" not in prompt


def test_system_prompt_defaults():
    prompt = build_system_prompt(ctx("some_new_state", flow=None))
    assert "an open question" in prompt
    assert "Slots currently offered to the user: none." in prompt


async def test_user_text_is_sent_verbatim():
    fake = FakeGenerate({"intent": "book_new", "confidence": 0.9})
    await GeminiNLU(fake).parse("book me in please", "intent_detect", ctx("intent_detect"))
    assert fake.calls[0][1] == "book me in please"


async def test_resolver_overrides_model_date():
    result = await run({
        "intent": "book_new", "confidence": 0.9, "topic": "SIP/Mandates",
        "date_text": "next Tuesday", "date_iso": "2026-10-13", "time_text": "after 4 pm",
    })  # fmt: skip
    assert result.source == "llm"
    assert result.intent is Intent.BOOK_NEW
    assert result.topic is Topic.SIP_MANDATES
    assert result.date_text == "next tuesday"
    assert result.date_iso == date(2026, 10, 6)  # resolver wins over the model's 13 Oct
    assert result.time_text == "after 16:00"


async def test_unparseable_day_keeps_model_hint():
    result = await run({"intent": "book_new", "confidence": 0.8,
                        "date_text": "the day after Diwali", "date_iso": "2026-11-09"})
    assert result.date_text == "the day after diwali"
    assert result.date_iso == date(2026, 11, 9)


async def test_date_iso_only():
    result = await run({"intent": "unknown", "confidence": 0.8, "date_iso": "2026-10-08"})
    assert (result.date_text, result.date_iso) == ("2026-10-08", date(2026, 10, 8))


async def test_any_day_has_no_date():
    result = await run({"intent": "unknown", "confidence": 0.8, "date_text": "whenever",
                        "date_iso": "2026-10-05", "time_text": "morning"})
    assert (result.date_text, result.date_iso, result.time_text) == ("any", None, "morning")


async def test_unparseable_time_is_dropped():
    result = await run({"intent": "unknown", "confidence": 0.8, "time_text": "sometime"})
    assert result.time_text is None


@pytest.mark.parametrize(
    ("choice", "offered", "expected"),
    [(2, OFFERED, 2), ("1", OFFERED, 1), (3, OFFERED, None), (2, OFFERED[:1], None),
     (1, [], None), ("first", OFFERED, None)],
)  # fmt: skip
async def test_slot_choice_must_be_offered(choice, offered, expected):
    result = await run({"intent": "unknown", "confidence": 0.9, "slot_choice": choice},
                       "offer_slots", offered)
    assert result.slot_choice == expected


@pytest.mark.parametrize(("code", "expected"), [("nl-a742", "NL-A742"), ("NL A742", "NL-A742"),
                                                ("ABC", None), ("NL-I111", None)])  # fmt: skip
async def test_booking_code_is_validated(code, expected):
    result = await run({"intent": "cancel", "confidence": 0.9, "booking_code": code})
    assert result.booking_code == expected


async def test_advice_clears_topic():
    result = await run({"intent": "investment_advice", "confidence": 0.9,
                        "topic": "SIP/Mandates"})
    assert result.intent is Intent.INVESTMENT_ADVICE
    assert result.topic is None


def test_extraction_is_lenient():
    x = GeminiExtraction.model_validate({
        "intent": "Book_New", "confidence": "1.7", "topic": "sip/mandates", "yes_no": "YES",
        "date_iso": "not a date", "slot_choice": "two", "time_text": "  ",
    })  # fmt: skip
    assert x.intent is Intent.BOOK_NEW
    assert x.confidence == 1.0
    assert x.topic is Topic.SIP_MANDATES
    assert x.yes_no is YesNo.YES
    assert x.date_iso is None and x.slot_choice is None and x.time_text is None


def test_extraction_unknown_values_degrade():
    x = GeminiExtraction.model_validate({"intent": "dance", "confidence": None, "topic": "Loans"})
    assert (x.intent, x.confidence, x.topic) == (Intent.UNKNOWN, 0.0, None)


async def test_code_fences_are_stripped():
    raw = '```json\n{"intent": "cancel", "confidence": 0.9}\n```'
    assert (await run(raw)).intent is Intent.CANCEL


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "", '{"intent": "book_new"'])
async def test_invalid_output_raises(raw):
    with pytest.raises(NLUUnavailable):
        await run(raw)


async def test_errors_raise_unavailable():
    nlu = GeminiNLU(FakeGenerate(exc=RuntimeError("quota exceeded")))
    with pytest.raises(NLUUnavailable):
        await nlu.parse("hi", "intent_detect", ctx("intent_detect"))


async def test_timeout_raises_unavailable():
    fake = FakeGenerate({"intent": "book_new", "confidence": 0.9}, delay=0.5)
    nlu = GeminiNLU(fake, timeout_s=0.05)
    with pytest.raises(NLUUnavailable):
        await nlu.parse("hi", "intent_detect", ctx("intent_detect"))
