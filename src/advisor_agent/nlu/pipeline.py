"""HybridNLU (LLD 3.7): rules first, Gemini only when the rules are not sufficient.

    rules = RulesNLU.parse(text)
    if sufficient(rules, state): return rules          # most turns: no LLM call, no latency
    llm = GeminiNLU.parse(text)  (on NLUUnavailable -> return rules)
    return merge(rules, llm)

Merge policy (per field): a value found by the rules wins (deterministic, 0.95); otherwise the
LLM value is used only if the LLM confidence >= threshold. The date fields (date_text, date_iso)
always come from a single source. An ambiguous rules topic (several candidates) is never
overridden - the orchestrator asks the user to choose.
"""

import logging

from advisor_agent.domain.models import Intent
from advisor_agent.nlu.date_resolver import ANY
from advisor_agent.nlu.engine import NluEngine
from advisor_agent.nlu.gemini import NLUUnavailable
from advisor_agent.nlu.rules import RULE_CONFIDENCE, RulesNLU, normalise
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo

log = logging.getLogger(__name__)

SHORT_ANSWER_TOKENS = 4
_YES_NO_STATES = {"disclaimer_ack", "confirm_slot", "cancel_confirm"}


def _definite(yn: YesNo | None) -> bool:
    return yn in (YesNo.YES, YesNo.NO)


def sufficient(rules: NLUResult, state: str, transcript: str) -> bool:
    """True when the rules result answers what the current state is asking for."""
    if rules.meta is not None:
        return True
    tokens = len(normalise(transcript).split())
    short = tokens <= SHORT_ANSWER_TOKENS
    has_date = rules.date_text is not None
    has_time = rules.time_text is not None
    has_pref = has_date or has_time
    match state:
        case s if s in _YES_NO_STATES:
            return _definite(rules.yes_no)
        case "ask_code":
            return rules.booking_code is not None
        case "offer_slots":
            return rules.slot_choice is not None or (
                short and (_definite(rules.yes_no) or has_pref)
            )
        case "topic_confirm" | "prep_topic":
            return rules.topic is not None or bool(rules.topic_candidates)
        case "intent_detect" | "clarify":
            return rules.intent is not None and rules.intent is not Intent.UNKNOWN
        case "collect_pref":
            return (has_date and has_time) or rules.date_text == ANY or (has_pref and short)
        case _:
            return rules.intent is not None and rules.intent is not Intent.UNKNOWN


def merge(rules: NLUResult, llm: NLUResult, threshold: float) -> NLUResult:
    rules_ok = rules.confidence >= RULE_CONFIDENCE
    llm_ok = llm.confidence >= threshold

    def pick(field: str) -> object:
        r, g = getattr(rules, field), getattr(llm, field)
        if rules_ok and r is not None:
            return r
        return g if llm_ok else r

    out = NLUResult(source="merged")

    r_intent = rules.intent if rules.intent is not Intent.UNKNOWN else None
    l_intent = llm.intent if llm.intent is not Intent.UNKNOWN else None
    if r_intent is not None and rules_ok:
        out.intent = r_intent
        agree = l_intent == r_intent
        out.confidence = max(rules.confidence, llm.confidence) if agree else rules.confidence
    elif l_intent is not None and llm_ok:
        out.intent, out.confidence = l_intent, llm.confidence
    else:
        out.intent = None

    if rules.topic_candidates and rules.topic is None:
        out.topic, out.topic_candidates = None, list(rules.topic_candidates)
    else:
        out.topic = pick("topic")  # type: ignore[assignment]

    if rules_ok and (rules.date_text is not None or rules.date_iso is not None):
        out.date_text, out.date_iso = rules.date_text, rules.date_iso
    elif llm_ok:
        out.date_text, out.date_iso = llm.date_text, llm.date_iso

    out.time_text = pick("time_text")  # type: ignore[assignment]
    out.slot_choice = pick("slot_choice")  # type: ignore[assignment]
    out.booking_code = pick("booking_code")  # type: ignore[assignment]
    yn = pick("yes_no")
    if rules_ok and rules.yes_no is YesNo.UNCLEAR and _definite(llm.yes_no) and llm_ok:
        yn = llm.yes_no
    out.yes_no = yn  # type: ignore[assignment]
    out.meta = rules.meta

    if out.intent is Intent.INVESTMENT_ADVICE:
        out.topic, out.topic_candidates = None, []

    if out.intent is None:
        signals = (out.topic, out.topic_candidates, out.date_text, out.time_text,
                   out.slot_choice, out.yes_no, out.booking_code)  # fmt: skip
        if any(signals):
            out.confidence = max(rules.confidence if rules_ok else 0.0,
                                 llm.confidence if llm_ok else 0.0)  # fmt: skip
        else:
            out.intent, out.confidence = Intent.UNKNOWN, 0.0
    return out


class HybridNLU:
    """Implements NluEngine: RulesNLU + optional GeminiNLU."""

    def __init__(
        self, rules: RulesNLU, llm: NluEngine | None = None, *, threshold: float = 0.7
    ) -> None:
        self.rules = rules
        self.llm = llm
        self.threshold = threshold
        self.llm_calls = 0  # observability / tests

    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        rules = await self.rules.parse(transcript, state, ctx)
        if self.llm is None or sufficient(rules, str(state), transcript):
            return rules
        self.llm_calls += 1
        try:
            llm = await self.llm.parse(transcript, state, ctx)
        except NLUUnavailable:
            return rules
        return merge(rules, llm, self.threshold)

    # LLD name
    understand = parse
