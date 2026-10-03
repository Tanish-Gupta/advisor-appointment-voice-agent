"""HybridNLU (LLD 3.7): sufficiency gate, merge policy, LLM fallback."""

from datetime import date, datetime

import pytest

from advisor_agent.domain.models import Intent, Topic
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.gemini import NLUUnavailable
from advisor_agent.nlu.pipeline import HybridNLU, merge, sufficient
from advisor_agent.nlu.rules import RULE_CONFIDENCE, RulesNLU
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
C = RULE_CONFIDENCE


def R(**kw) -> NLUResult:  # noqa: N802 - a rules result
    return NLUResult(source="rules", confidence=kw.pop("confidence", C), **kw)


def L(**kw) -> NLUResult:  # noqa: N802 - an LLM result
    return NLUResult(source="llm", confidence=kw.pop("confidence", 0.9), **kw)


@pytest.mark.parametrize(
    ("rules", "state", "text", "expected"),
    [
        (R(meta="repeat"), "collect_pref", "repeat", True),
        (R(yes_no=YesNo.YES), "disclaimer_ack", "yes", True),
        (R(yes_no=YesNo.UNCLEAR), "confirm_slot", "yes but no", False),
        (R(), "confirm_slot", "hmm let me think", False),
        (R(booking_code="NL-A742"), "ask_code", "NL-A742", True),
        (R(), "ask_code", "it starts with A I think", False),
        (R(slot_choice=2), "offer_slots", "I'd prefer the second one if possible", True),
        (R(yes_no=YesNo.NO), "offer_slots", "neither", True),
        (R(yes_no=YesNo.NO), "offer_slots", "no I am not sure any of those will work", False),
        (R(date_text="thursday"), "offer_slots", "thursday instead", True),
        (R(topic=Topic.SIP_MANDATES), "topic_confirm", "sip", True),
        (R(topic_candidates=[Topic.SIP_MANDATES, Topic.WITHDRAWALS]), "topic_confirm",
         "sip withdrawal", True),
        (R(), "topic_confirm", "the thing with my money", False),
        (R(intent=Intent.BOOK_NEW), "intent_detect", "book", True),
        (R(intent=Intent.UNKNOWN, confidence=0.0), "intent_detect", "hmm", False),
        (R(), "clarify", "something", False),
        (R(date_text="tuesday", time_text="afternoon"), "collect_pref",
         "tuesday afternoon would be good if that suits", True),
        (R(date_text="any"), "collect_pref", "honestly whenever suits the advisor best", True),
        (R(date_text="tuesday"), "collect_pref", "tuesday", True),
        (R(date_text="tuesday"), "collect_pref", "maybe tuesday if that is okay with you", False),
        (R(), "collect_pref", "after my office meeting finishes", False),
    ],
)  # fmt: skip
def test_sufficient(rules, state, text, expected):
    assert sufficient(rules, state, text) is expected


def test_rules_intent_wins():
    out = merge(R(intent=Intent.BOOK_NEW), L(intent=Intent.CANCEL), 0.7)
    assert (out.intent, out.confidence, out.source) == (Intent.BOOK_NEW, C, "merged")


def test_agreement_takes_max_confidence():
    out = merge(R(intent=Intent.BOOK_NEW), L(intent=Intent.BOOK_NEW, confidence=0.99), 0.7)
    assert out.confidence == 0.99


def test_llm_fills_missing_intent_and_fields():
    out = merge(
        R(intent=Intent.UNKNOWN, confidence=0.0),
        L(intent=Intent.BOOK_NEW, topic=Topic.WITHDRAWALS, time_text="afternoon"),
        0.7,
    )
    assert (out.intent, out.topic, out.time_text) == (
        Intent.BOOK_NEW, Topic.WITHDRAWALS, "afternoon"
    )
    assert out.confidence == 0.9


def test_low_confidence_llm_is_ignored():
    out = merge(R(intent=Intent.UNKNOWN, confidence=0.0),
                L(intent=Intent.BOOK_NEW, topic=Topic.WITHDRAWALS, confidence=0.4), 0.7)
    assert (out.intent, out.confidence, out.topic) == (Intent.UNKNOWN, 0.0, None)


def test_rules_field_wins_llm_fills_gaps():
    out = merge(R(topic=Topic.SIP_MANDATES), L(topic=Topic.KYC_ONBOARDING, slot_choice=1), 0.7)
    assert out.topic is Topic.SIP_MANDATES
    assert out.slot_choice == 1
    assert out.intent is None and out.confidence == C  # signals present, no intent


def test_ambiguous_rules_topic_is_never_overridden():
    cands = [Topic.SIP_MANDATES, Topic.STATEMENTS_TAX]
    out = merge(R(topic_candidates=cands), L(topic=Topic.SIP_MANDATES), 0.7)
    assert out.topic is None
    assert out.topic_candidates == cands


def test_date_pair_from_one_source():
    rules = R(date_text="tuesday", date_iso=date(2026, 10, 6))
    llm = L(date_text="wednesday", date_iso=date(2026, 10, 7), time_text="morning")
    out = merge(rules, llm, 0.7)
    assert (out.date_text, out.date_iso, out.time_text) == (
        "tuesday", date(2026, 10, 6), "morning"
    )
    out = merge(R(time_text="morning"), llm, 0.7)
    assert (out.date_text, out.date_iso, out.time_text) == (
        "wednesday", date(2026, 10, 7), "morning"
    )


def test_unclear_yes_no_resolved_by_llm():
    out = merge(R(yes_no=YesNo.UNCLEAR), L(yes_no=YesNo.YES), 0.7)
    assert out.yes_no is YesNo.YES
    out = merge(R(yes_no=YesNo.NO), L(yes_no=YesNo.YES), 0.7)
    assert out.yes_no is YesNo.NO


def test_advice_clears_topic_after_merge():
    out = merge(R(topic=Topic.SIP_MANDATES), L(intent=Intent.INVESTMENT_ADVICE), 0.7)
    assert out.intent is Intent.INVESTMENT_ADVICE
    assert out.topic is None


def test_meta_comes_from_rules():
    assert merge(R(meta="help"), L(), 0.7).meta == "help"


class FakeLLM:
    def __init__(self, result: NLUResult | None = None, *, fail: bool = False):
        self.result, self.fail, self.calls = result, fail, 0

    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        self.calls += 1
        if self.fail:
            raise NLUUnavailable("down")
        assert self.result is not None
        return self.result


def ctx(state: str) -> NLUContext:
    return NLUContext(state=state, ref=REF)


VAGUE = "I'd like to sort out something about my money"


async def test_rules_only_without_llm():
    nlu = HybridNLU(RulesNLU())
    result = await nlu.parse(VAGUE, "intent_detect", ctx("intent_detect"))
    assert result.intent is Intent.UNKNOWN and result.source == "rules"


async def test_llm_skipped_when_rules_suffice():
    llm = FakeLLM(L(intent=Intent.CANCEL))
    nlu = HybridNLU(RulesNLU(), llm)
    result = await nlu.parse("I want to book an appointment", "intent_detect", ctx("intent_detect"))
    assert result.intent is Intent.BOOK_NEW
    assert llm.calls == 0 and nlu.llm_calls == 0


async def test_llm_used_when_rules_insufficient():
    llm = FakeLLM(L(intent=Intent.BOOK_NEW, topic=Topic.WITHDRAWALS))
    nlu = HybridNLU(RulesNLU(), llm, threshold=0.7)
    result = await nlu.parse(VAGUE, "intent_detect", ctx("intent_detect"))
    assert (result.intent, result.topic, result.source) == (
        Intent.BOOK_NEW, Topic.WITHDRAWALS, "merged"
    )
    assert llm.calls == 1 and nlu.llm_calls == 1


async def test_falls_back_to_rules_when_llm_unavailable():
    nlu = HybridNLU(RulesNLU(), FakeLLM(fail=True))
    result = await nlu.parse(VAGUE, "intent_detect", ctx("intent_detect"))
    assert result.source == "rules" and result.intent is Intent.UNKNOWN
    assert nlu.llm_calls == 1


async def test_understand_alias():
    nlu = HybridNLU(RulesNLU())
    result = await nlu.understand("book", "intent_detect", ctx("intent_detect"))
    assert result.intent is Intent.BOOK_NEW
