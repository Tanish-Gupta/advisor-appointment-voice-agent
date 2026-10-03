"""Golden intent fixtures (LLD 3.8 / Phase 3 exit criteria): every label, runnable without voice
or network. Each phrase goes through RulesNLU and through HybridNLU with an LLM that knows
nothing, proving the rules alone cover the fixtures."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from advisor_agent.domain.models import Intent
from advisor_agent.domain.timeutil import IST
from advisor_agent.nlu.pipeline import HybridNLU
from advisor_agent.nlu.rules import RulesNLU
from advisor_agent.nlu.schema import NLUContext, NLUResult

REF = datetime(2026, 10, 2, 16, 0, tzinfo=IST)
GOLDEN: dict[str, list[str]] = yaml.safe_load(
    (Path(__file__).parent / "golden_intents.yaml").read_text(encoding="utf-8")
)
CASES = [(label, text) for label, texts in GOLDEN.items() for text in texts]
LABELS = {"book_new", "reschedule", "cancel", "what_to_prepare", "check_availability",
          "investment_advice", "unknown"}  # fmt: skip


def test_every_label_has_enough_fixtures():
    assert set(GOLDEN) == LABELS
    for label, texts in GOLDEN.items():
        assert len(texts) >= 20, label
        assert len(set(texts)) == len(texts), label


@pytest.mark.parametrize(("label", "text"), CASES)
def test_rules_golden(label, text):
    result = RulesNLU().parse_sync(text, "intent_detect", [], REF)
    assert result.intent is Intent(label), text


class UnknownLLM:
    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        return NLUResult(intent=Intent.UNKNOWN, confidence=0.2, source="llm")


@pytest.mark.parametrize(("label", "text"), CASES)
async def test_hybrid_golden(label, text):
    nlu = HybridNLU(RulesNLU(), UnknownLLM())
    result = await nlu.parse(text, "intent_detect", NLUContext(state="intent_detect", ref=REF))
    assert result.intent is Intent(label), text
    # the LLM is consulted only when the rules could not tell
    assert nlu.llm_calls == (1 if label == "unknown" else 0)
