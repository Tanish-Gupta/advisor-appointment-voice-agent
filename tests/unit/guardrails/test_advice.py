"""Advice detector (LLD 5.2): advice is caught, process questions are not."""

import pytest

from advisor_agent.guardrails.advice import detect_advice


@pytest.mark.parametrize(
    "text",
    [
        "Which fund gives the best returns?",
        "should I buy HDFC shares?",
        "Should I sell my gold?",
        "can you recommend a good mutual fund",
        "what are the best SIPs right now",
        "is gold a good investment",
        "where should I invest 1 lakh",
        "is now a good time to buy?",
        "will the market crash next month",
        "any stock tips?",
        "what returns will I get",
    ],
)
def test_advice_is_detected(text: str) -> None:
    assert detect_advice(text).is_advice


@pytest.mark.parametrize(
    "text",
    [
        "When will my withdrawal money arrive?",
        "How do I change my SIP date?",
        "what is the best SIP date to pick",
        "I want to book a slot about tax statements",
        "will my returns be taxed",
        "what should I bring for KYC?",
        "cancel my booking NL-A742",
        "what's free on monday afternoon",
        "I'd like to update my nominee",
        "yes",
    ],
)
def test_process_questions_are_not_advice(text: str) -> None:
    assert not detect_advice(text).is_advice
