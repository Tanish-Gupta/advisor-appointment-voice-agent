"""Gemini NLU (LLD 3.3 / 3.6): structured-output extraction with the Google Gen AI SDK.

The LLM is used for *understanding only*: it returns a JSON object matching RESPONSE_SCHEMA and
never writes user-facing text. Its output is sanitised before use:
- `date_text` is re-resolved by the deterministic RelativeDateResolver (source of truth);
  the model's `date_iso` is only a fallback hint,
- `time_text` is canonicalised (or dropped if it cannot be parsed),
- `slot_choice` must refer to an offered slot, booking codes must match NL-X999.

`google-genai` is imported lazily so tests and the stub/rules engines run without it.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from advisor_agent.domain import codes
from advisor_agent.domain.models import Intent, Topic
from advisor_agent.nlu.date_resolver import ANY, RelativeDateResolver, extract_date, extract_time
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo

log = logging.getLogger(__name__)

# (system_instruction, user_text) -> raw JSON text
GenerateFn = Callable[[str, str], Awaitable[str]]


class NLUUnavailable(Exception):
    """The LLM could not be reached or returned unusable output; callers fall back to rules."""


# OpenAPI-style schema accepted by Gemini structured output (response_schema).
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": [i.value for i in Intent]},
        "confidence": {"type": "NUMBER"},
        "topic": {"type": "STRING", "enum": [t.value for t in Topic], "nullable": True},
        "date_text": {"type": "STRING", "nullable": True},
        "date_iso": {"type": "STRING", "nullable": True},
        "time_text": {"type": "STRING", "nullable": True},
        "slot_choice": {"type": "INTEGER", "nullable": True},
        "yes_no": {"type": "STRING", "enum": [y.value for y in YesNo], "nullable": True},
        "booking_code": {"type": "STRING", "nullable": True},
    },
    "required": ["intent", "confidence"],
    "propertyOrdering": [
        "intent", "confidence", "topic", "date_text", "date_iso", "time_text", "slot_choice",
        "yes_no", "booking_code",
    ],
}  # fmt: skip

STATE_QUESTIONS: dict[str, str] = {
    "disclaimer_ack": "whether they understand the disclaimer and want to continue (yes/no)",
    "intent_detect": "how we can help (book, reschedule, cancel, what to prepare, availability)",
    "clarify": "which of: book, reschedule, cancel, what to prepare, check availability",
    "topic_confirm": "which consultation topic (1-5) they want to discuss",
    "collect_pref": "which day and time (IST) suits them",
    "offer_slots": "which of the offered slots they want (1 or 2), or neither",
    "confirm_slot": "to confirm placing the tentative hold (yes/no)",
    "ask_code": "their booking code (format NL-A742)",
    "cancel_confirm": "to confirm cancelling the booking (yes/no)",
    "prep_topic": "which topic they want preparation tips for",
}

SYSTEM_TEMPLATE = """\
You are the language-understanding component of an advisor appointment scheduler for a \
mutual-fund / investment platform in India. You do NOT talk to the user. You only convert the \
user's latest message into one JSON object that follows the response schema.

Context:
- Today is {today}; current time {now_time}. All times are IST (Asia/Kolkata).
- Advisors work Monday to Friday, 9:00 AM to 6:00 PM IST.
- Conversation state: {state} (flow: {flow}). The assistant just asked the user {question}.
- Slots currently offered to the user: {offered_slots}.

Fields:
- intent: one of book_new, reschedule, cancel, what_to_prepare, check_availability, \
investment_advice, small_talk, unknown. A bare answer to the assistant's question (a topic, a \
day/time, a slot choice, yes/no) is book_new only if it clearly continues a booking; otherwise \
use unknown and fill the matching field.
- topic: exactly one of "KYC/Onboarding", "SIP/Mandates", "Statements/Tax Docs", \
"Withdrawals & Timelines", "Account Changes/Nominee". If the user mentions several topics \
equally, or none, use null.
- date_text: copy the user's day words verbatim (e.g. "next tuesday", "tomorrow", "7 oct"); \
"any" if they have no day preference. date_iso: that day as YYYY-MM-DD, or null.
- time_text: copy the time words verbatim (e.g. "afternoon", "around 3 pm", "after 4", \
"between 2 and 4 pm"); null if none.
- slot_choice: 1 or 2 only if the user picks one of the offered slots; null otherwise.
- yes_no: "yes", "no" or "unclear" only when the user answers a yes/no question or rejects \
the offered slots ("neither" = "no"); null otherwise.
- booking_code: format NL-X999 (e.g. NL-A742) if the user states one, else null.
- confidence: 0..1. Use below 0.5 when the message is ambiguous.

Rules:
- Requests for investment advice, recommendations, market predictions or "should I buy/sell" \
=> intent investment_advice. Never give advice yourself.
- "tax returns", "statements" and "capital gains" are the Statements/Tax Docs topic, not advice.
- Text may contain markers like [REDACTED:phone]; ignore them.
- Output JSON only. No explanations."""


def build_system_prompt(ctx: NLUContext) -> str:
    v = ctx.prompt_vars()
    question = STATE_QUESTIONS.get(v["state"], "an open question")
    return SYSTEM_TEMPLATE.format(question=question, **v)


def _enum_or_none(enum: type, value: Any) -> Any:
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        if isinstance(value, str):
            for member in enum:
                if member.value.lower() == value.strip().lower():
                    return member
        return None


class GeminiExtraction(BaseModel):
    """Lenient parse of the model output (bad enum values degrade to None/unknown)."""

    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    topic: Topic | None = None
    date_text: str | None = None
    date_iso: date | None = None
    time_text: str | None = None
    slot_choice: int | None = None
    yes_no: YesNo | None = None
    booking_code: str | None = None

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v: Any) -> Intent:
        return _enum_or_none(Intent, v) or Intent.UNKNOWN

    @field_validator("topic", mode="before")
    @classmethod
    def _topic(cls, v: Any) -> Topic | None:
        return _enum_or_none(Topic, v)

    @field_validator("yes_no", mode="before")
    @classmethod
    def _yes_no(cls, v: Any) -> YesNo | None:
        return _enum_or_none(YesNo, v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> float:
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return 0.0

    @field_validator("date_iso", mode="before")
    @classmethod
    def _date_iso(cls, v: Any) -> date | None:
        if isinstance(v, str):
            try:
                return date.fromisoformat(v.strip()[:10])
            except ValueError:
                return None
        return v if isinstance(v, date) else None

    @field_validator("slot_choice", mode="before")
    @classmethod
    def _slot_choice(cls, v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @field_validator("date_text", "time_text", "booking_code", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    return s.strip()


class GeminiNLU:
    """Implements NluEngine on top of a `GenerateFn` (real Gemini or a test fake)."""

    def __init__(
        self,
        generate: GenerateFn,
        *,
        timeout_s: float = 4.0,
        resolver: RelativeDateResolver | None = None,
    ) -> None:
        self._generate = generate
        self._timeout_s = timeout_s
        self.resolver = resolver or RelativeDateResolver()

    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        system = build_system_prompt(ctx)
        try:
            raw = await asyncio.wait_for(self._generate(system, transcript), self._timeout_s)
        except Exception as e:  # timeout, network, quota, SDK errors
            log.warning("gemini nlu unavailable: %s", type(e).__name__)
            raise NLUUnavailable(type(e).__name__) from e
        try:
            data = json.loads(_strip_fences(raw or ""))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            extraction = GeminiExtraction.model_validate(data)
        except (ValueError, ValidationError) as e:
            log.warning("gemini nlu returned invalid JSON")
            raise NLUUnavailable("invalid_output") from e
        return self.sanitise(extraction, ctx)

    def sanitise(self, x: GeminiExtraction, ctx: NLUContext) -> NLUResult:
        date_text: str | None = None
        date_iso: date | None = None
        if x.date_text:
            canonical = extract_date(x.date_text.lower())
            if canonical is not None:
                date_text = canonical
                date_iso = self.resolver.resolve_date(canonical, ctx.ref)
            else:
                date_text = x.date_text.lower()
                date_iso = x.date_iso
        elif x.date_iso is not None:
            date_text, date_iso = x.date_iso.isoformat(), x.date_iso
        if date_text == ANY:
            date_iso = None

        time_text = extract_time(x.time_text.lower()) if x.time_text else None

        n_offered = len(ctx.offered_slots)
        slot_choice = x.slot_choice if x.slot_choice in (1, 2) else None
        if slot_choice is not None and slot_choice > n_offered:
            slot_choice = None

        code = codes.parse_spoken_or_typed(x.booking_code) if x.booking_code else None

        return NLUResult(
            intent=x.intent,
            confidence=x.confidence,
            topic=x.topic if x.intent is not Intent.INVESTMENT_ADVICE else None,
            date_text=date_text,
            date_iso=date_iso,
            time_text=time_text,
            slot_choice=slot_choice,  # type: ignore[arg-type]
            yes_no=x.yes_no,
            booking_code=code,
            source="llm",
        )


def google_generate(
    api_key: str,
    model: str,
    *,
    thinking_level: str | None = None,
    max_output_tokens: int = 1024,
) -> GenerateFn:
    """Build a GenerateFn backed by the Google Gen AI SDK (`pip install google-genai`).

    Raises ImportError if the SDK is missing (build_engine then degrades to rules-only).
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    use_thinking = {"enabled": bool(thinking_level)}

    def _config(system: str) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA,
            "temperature": 0.0,
            "max_output_tokens": max_output_tokens,
        }
        if use_thinking["enabled"]:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        return types.GenerateContentConfig(**kwargs)

    async def generate(system: str, user_text: str) -> str:
        try:
            resp = await client.aio.models.generate_content(
                model=model, contents=user_text, config=_config(system)
            )
        except Exception as e:
            # Older models / SDKs reject thinking_level: retry once without it, then remember.
            if not use_thinking["enabled"] or "think" not in str(e).lower():
                raise
            use_thinking["enabled"] = False
            resp = await client.aio.models.generate_content(
                model=model, contents=user_text, config=_config(system)
            )
        return resp.text or ""

    return generate
