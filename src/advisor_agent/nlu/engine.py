"""NluEngine interface (LLD 3.1) and engine factory.

AGENT_NLU_ENGINE:
- stub   -> StubNLU (typed commands; Phase 2 tests)
- rules  -> RulesNLU (deterministic, offline)
- hybrid -> HybridNLU = RulesNLU + GeminiNLU when a Gemini API key and `google-genai` are
            available; otherwise it logs a warning and runs rules-only.
"""

import logging
from typing import Protocol

from advisor_agent.config import Settings
from advisor_agent.nlu.schema import NLUContext, NLUResult

log = logging.getLogger(__name__)


class NluEngine(Protocol):
    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult: ...


def build_engine(settings: Settings) -> NluEngine:
    if settings.nlu_engine == "stub":
        from advisor_agent.nlu.stub import StubNLU

        return StubNLU()

    from advisor_agent.nlu.date_resolver import RelativeDateResolver
    from advisor_agent.nlu.rules import RulesNLU

    resolver = RelativeDateResolver(
        horizon_days=settings.booking_horizon_days,
        business_hours=settings.business_hours,
        lead_time_min=settings.lead_time_min,
        slot_minutes=settings.slot_duration_min,
    )
    rules = RulesNLU(resolver)
    if settings.nlu_engine == "rules":
        return rules
    if settings.nlu_engine != "hybrid":
        raise ValueError(f"unknown NLU engine {settings.nlu_engine!r} (stub | rules | hybrid)")

    from advisor_agent.nlu.gemini import GeminiNLU, google_generate
    from advisor_agent.nlu.pipeline import HybridNLU

    llm: GeminiNLU | None = None
    key = settings.gemini_api_key.get_secret_value().strip() if settings.gemini_api_key else ""
    if not key:
        log.warning("hybrid NLU: no Gemini API key (AGENT_GEMINI_API_KEY); running rules-only")
    else:
        try:
            generate = google_generate(
                key,
                settings.gemini_model,
                thinking_level=settings.gemini_thinking_level,
            )
            llm = GeminiNLU(generate, timeout_s=settings.nlu_timeout_s, resolver=resolver)
        except ImportError:
            log.warning("hybrid NLU: google-genai is not installed; running rules-only")
    return HybridNLU(rules, llm, threshold=settings.nlu_confidence_threshold)
