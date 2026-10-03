"""Investment-advice detector (LLD 5.2): the global advice interrupt.

Stage 1 is these rules (also used by RulesNLU for the `investment_advice` intent); stage 2 is
the NLU intent with confidence >= threshold, combined by the orchestrator. Process questions
("when will my withdrawal arrive?", "how do I change my SIP date?") must NOT match.
"""

import re
from dataclasses import dataclass

ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bshould i (?:buy|sell|invest|redeem|switch|hold|stop my|start|exit|put"
               r"|move my money|book profit)"),
    re.compile(r"\b(?:recommend|suggest)\w*\b[a-z0-9 ']{0,30}\b(?:funds?|stocks?|shares?"
               r"|schemes?|portfolio|gold|crypto|bitcoin|equity|investments?|ipos?|nfos?)\b"),
    re.compile(r"\b(?:which|what) (?:mutual )?(?:funds?|stocks?|shares?|schemes?|sips?|ipos?"
               r"|nfos?|investments?) (?:should|to|is|are|would|will|can|do)\b"),
    re.compile(r"\b(?:best|top|good|better|safest|safe|high[- ]return)\b(?: [a-z]+){0,2}"
               r" (?:funds?|stocks?|shares?|schemes?|investments?|ipos?|portfolio"
               r"|sips?(?! dates?))\b"),
    re.compile(r"\b(?:investment|financial|stock|share|trading|tax[- ]saving|portfolio)"
               r" (?:advice|tips?|recommendations?|suggestions?|ideas?)\b"),
    re.compile(r"\b(?:where|what|how much|how) (?:should|can|do) i invest\b"),
    re.compile(r"\bis (?:it|this|now) (?:a )?(?:good|right|bad|the right) time to"
               r" (?:buy|sell|invest|redeem|exit|enter)\b"),
    re.compile(r"\b(?:will|would|is) (?:the )?(?:market|nifty|sensex|stock|price|nav)s?"
               r" (?:go|rise|fall|crash|recover|going)\b"),
    re.compile(r"\b(?:guaranteed|expected|best|highest|good|better|maximum) returns?\b"),
    re.compile(r"\bbuy or sell\b|\bstock tips?\b|\bmultibagger\b"),
    re.compile(r"\b(?:returns?|navs?) (?:will|would|is going to|are going to)\b"),
    re.compile(r"\bis (?:[a-z]+ ){1,4}(?:a )?(?:good|safe|bad|risky)"
               r" (?:investment|to invest|bet)\b"),
)  # fmt: skip


@dataclass(frozen=True)
class AdviceSignal:
    is_advice: bool
    reason: str = ""


def _normalise(text: str) -> str:
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"[^a-z0-9\s:/'&-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def detect_advice(text: str) -> AdviceSignal:
    t = _normalise(text)
    for i, pattern in enumerate(ADVICE_PATTERNS):
        if pattern.search(t):
            return AdviceSignal(True, f"rule:{i}")
    return AdviceSignal(False)
