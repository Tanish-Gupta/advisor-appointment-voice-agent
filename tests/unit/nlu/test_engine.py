"""build_engine factory (LLD 3.1): engine selection and graceful degradation without a key."""

import pytest
from pydantic import SecretStr

from advisor_agent.nlu import engine as engine_mod
from advisor_agent.nlu.engine import build_engine
from advisor_agent.nlu.pipeline import HybridNLU
from advisor_agent.nlu.rules import RulesNLU
from advisor_agent.nlu.stub import StubNLU
from tests.conftest import make_settings


def test_stub():
    assert isinstance(build_engine(make_settings(nlu_engine="stub")), StubNLU)


def test_rules_uses_settings_for_resolver():
    nlu = build_engine(make_settings(nlu_engine="rules", booking_horizon_days=7))
    assert isinstance(nlu, RulesNLU)
    assert nlu.resolver.horizon_days == 7


def test_hybrid_without_key_runs_rules_only():
    nlu = build_engine(make_settings(nlu_engine="hybrid", nlu_confidence_threshold=0.8))
    assert isinstance(nlu, HybridNLU)
    assert nlu.llm is None
    assert nlu.threshold == 0.8


def test_hybrid_with_blank_key_runs_rules_only():
    nlu = build_engine(make_settings(nlu_engine="hybrid", gemini_api_key=SecretStr("  ")))
    assert isinstance(nlu, HybridNLU)
    assert nlu.llm is None


def test_hybrid_with_key_but_no_sdk_degrades(monkeypatch: pytest.MonkeyPatch):
    import advisor_agent.nlu.gemini as gemini

    def missing_sdk(*args: object, **kwargs: object) -> None:
        raise ImportError("google-genai")

    monkeypatch.setattr(gemini, "google_generate", missing_sdk)
    nlu = build_engine(make_settings(nlu_engine="hybrid", gemini_api_key=SecretStr("k")))
    assert isinstance(nlu, HybridNLU)
    assert nlu.llm is None


def test_hybrid_with_key_wires_gemini(monkeypatch: pytest.MonkeyPatch):
    import advisor_agent.nlu.gemini as gemini

    seen: dict[str, object] = {}

    def fake_generate(api_key: str, model: str, **kwargs: object):
        seen.update(api_key=api_key, model=model, **kwargs)

        async def generate(system: str, user: str) -> str:
            return "{}"

        return generate

    monkeypatch.setattr(gemini, "google_generate", fake_generate)
    nlu = build_engine(
        make_settings(
            nlu_engine="hybrid",
            gemini_api_key=SecretStr("secret"),
            gemini_model="gemini-test",
            gemini_thinking_level="low",
        )
    )
    assert isinstance(nlu, HybridNLU)
    assert isinstance(nlu.llm, gemini.GeminiNLU)
    assert seen == {"api_key": "secret", "model": "gemini-test", "thinking_level": "low"}


def test_unknown_engine():
    with pytest.raises(ValueError, match="unknown NLU engine"):
        build_engine(make_settings(nlu_engine="bogus"))


def test_engine_module_exports_protocol():
    assert hasattr(engine_mod, "NluEngine")
