"""Stub NLU (LLD 2.4): deterministic typed commands / literal phrases. No LLM, no I/O.

Kept after Phase 3 for fast, deterministic tests (AGENT_NLU_ENGINE=stub).
"""

import re
from datetime import date, timedelta

from advisor_agent.domain import codes
from advisor_agent.domain.models import Intent, Topic
from advisor_agent.domain.timeutil import to_ist
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo

_YES = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "yes please", "i understand", "i agree"}
_NO = {"no", "n", "nope", "no thanks", "neither", "none", "neither of them", "none of them"}
_META = {
    "repeat": "repeat",
    "say again": "repeat",
    "again": "repeat",
    "help": "help",
    "commands": "help",
    "stop": "stop",
    "bye": "stop",
    "goodbye": "stop",
    "exit": "stop",
    "quit": "stop",
}

_TOPIC_ORDER = list(Topic)  # 1..5 in ASK_TOPIC order
_TOPIC_ALIASES: list[tuple[str, Topic]] = [
    ("kyc", Topic.KYC_ONBOARDING),
    ("onboarding", Topic.KYC_ONBOARDING),
    ("sip", Topic.SIP_MANDATES),
    ("mandate", Topic.SIP_MANDATES),
    ("statement", Topic.STATEMENTS_TAX),
    ("tax", Topic.STATEMENTS_TAX),
    ("withdraw", Topic.WITHDRAWALS),
    ("timeline", Topic.WITHDRAWALS),
    ("nominee", Topic.ACCOUNT_CHANGES),
    ("account change", Topic.ACCOUNT_CHANGES),
]

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}  # fmt: skip
_PARTS = ("morning", "afternoon", "evening")
_ANY = {"any", "anytime", "any time", "any day", "whenever", "earliest"}

_SLOT_CHOICE = {
    "1": 1, "first": 1, "option 1": 1, "slot 1": 1, "first slot": 1, "the first one": 1,
    "first one": 1, "the first": 1, "1st": 1,
    "2": 2, "second": 2, "option 2": 2, "slot 2": 2, "second slot": 2, "the second one": 2,
    "second one": 2, "the second": 2, "2nd": 2,
}  # fmt: skip

_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_SEGMENT_RE = re.compile(r"\b(topic|time)\s+(.+?)(?=\s+\b(?:topic|time)\b|$)")


def _normalise(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[!?.,;]+$", "", text)
    return re.sub(r"\s+", " ", text)


def match_topic(text: str) -> Topic | None:
    t = text.strip().lower()
    if t.isdigit() and 1 <= int(t) <= len(_TOPIC_ORDER):
        return _TOPIC_ORDER[int(t) - 1]
    for topic in Topic:  # exact label (quick-reply chips send these)
        if t == topic.value.lower():
            return topic
    for alias, topic in _TOPIC_ALIASES:
        if alias in t:
            return topic
    return None


def parse_time(text: str, today: date) -> tuple[str | None, date | None, str | None]:
    """Return (date_text, date_iso, time_text) for 'tuesday afternoon', '2026-10-06 morning',
    'tomorrow', 'any'. A weekday resolves to its next occurrence (today counts)."""
    t = text.strip().lower()
    if t in _ANY:
        return "any", None, None
    date_text: str | None = None
    date_iso: date | None = None
    if m := _ISO_RE.search(t):
        try:
            date_iso = date.fromisoformat(m.group(1))
            date_text = m.group(1)
        except ValueError:
            pass
    words = re.findall(r"[a-z]+", t)
    if date_iso is None:
        for w in words:
            if w == "today":
                date_text, date_iso = w, today
            elif w == "tomorrow":
                date_text, date_iso = w, today + timedelta(days=1)
            elif w in _WEEKDAYS:
                date_text = w
                date_iso = today + timedelta(days=(_WEEKDAYS[w] - today.weekday()) % 7)
            if date_iso is not None:
                break
    time_text = next((w for w in words if w in _PARTS), None)
    return date_text, date_iso, time_text


class StubNLU:
    """Implements NluEngine. Replaced by HybridNLU in Phase 3."""

    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        text = _normalise(transcript)
        today = to_ist(ctx.ref).date()

        if text in _META:
            return NLUResult(meta=_META[text], confidence=1.0, source="stub")  # type: ignore[arg-type]
        if text in _YES:
            return NLUResult(yes_no=YesNo.YES, confidence=1.0, source="stub")
        if text in _NO:
            return NLUResult(yes_no=YesNo.NO, confidence=1.0, source="stub")

        result = NLUResult(source="stub")

        # Intents (first word), optionally followed by 'topic X' / 'time Y' segments.
        first = text.split(" ", 1)[0] if text else ""
        if first in {"book", "booking"}:
            result.intent = Intent.BOOK_NEW
        elif first == "reschedule":
            result.intent = Intent.RESCHEDULE
        elif first == "cancel":
            result.intent = Intent.CANCEL
        elif first in {"prepare", "preparation"} or text.startswith("what to prepare"):
            result.intent = Intent.WHAT_TO_PREPARE
        elif first == "availability" or text.startswith("check availability"):
            result.intent = Intent.CHECK_AVAILABILITY
        if result.intent is not None:
            result.confidence = 1.0
            if result.intent in {Intent.RESCHEDULE, Intent.CANCEL}:
                result.booking_code = codes.parse_spoken_or_typed(transcript)

        segments = {k: v for k, v in _SEGMENT_RE.findall(text)}
        if "topic" in segments:
            result.topic = match_topic(segments["topic"])
        if "time" in segments:
            result.date_text, result.date_iso, result.time_text = parse_time(
                segments["time"], today
            )

        # Bare answers, interpreted by the state that asked the question.
        if result.intent is None and not segments:
            if state in {"topic_confirm", "prep_topic"}:
                result.topic = match_topic(text)
            elif state == "collect_pref":
                result.date_text, result.date_iso, result.time_text = parse_time(text, today)
            elif state == "ask_code":
                result.booking_code = codes.parse_spoken_or_typed(transcript)
            elif state == "offer_slots":
                result.slot_choice = _SLOT_CHOICE.get(text)  # type: ignore[assignment]
                if result.slot_choice is None:
                    result.date_text, result.date_iso, result.time_text = parse_time(text, today)

        if result.intent is None and (
            result.topic
            or result.date_text
            or result.time_text
            or result.slot_choice
            or result.booking_code
        ):
            result.confidence = 1.0
        if result.intent is None and result.confidence == 0.0:
            result.intent = Intent.UNKNOWN
        return result
