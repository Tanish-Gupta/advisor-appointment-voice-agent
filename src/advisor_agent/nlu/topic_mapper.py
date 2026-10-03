"""TopicMapper (LLD 3.4): free text → one of the 5 consultation topics.

Word-boundary synonym matching; the topic with the most hits wins. A tie means the user mentioned
several topics ("tax on my SIP withdrawal"), so the result is ambiguous and the orchestrator asks
them to choose (TOPIC_OPTIONS) instead of guessing.
"""

import re

from advisor_agent.domain.models import Topic

TOPIC_ORDER: list[Topic] = list(Topic)  # 1..5, the order used by ASK_TOPIC

TOPIC_SYNONYMS: dict[Topic, list[str]] = {
    Topic.KYC_ONBOARDING: [
        "kyc", "ekyc", "e-kyc", "know your customer", "onboarding", "onboard",
        "open an account", "open account", "opening an account", "account opening",
        "new account", "sign up", "signup", "register", "registration", "verification",
        "verify my", "aadhaar", "aadhar", "pan card", "pan", "ckyc", "in-person verification",
        "ipv", "video kyc",
    ],
    Topic.SIP_MANDATES: [
        "sip", "sips", "systematic investment plan", "mandate", "mandates", "e-mandate",
        "emandate", "nach", "autopay", "auto pay", "auto-debit", "auto debit", "autodebit",
        "standing instruction", "monthly investment", "step up", "step-up", "pause sip",
        "debit date",
    ],
    Topic.STATEMENTS_TAX: [
        "statement", "statements", "account statement", "cas", "consolidated account statement",
        "capital gains", "capital gain", "tax", "taxes", "tax docs", "tax documents",
        "tax document", "tax certificate", "tax statement", "elss", "form 16", "26as",
        "itr", "tax return", "tax returns", "contract note", "contract notes", "p&l",
        "holding statement", "transaction history",
    ],
    Topic.WITHDRAWALS: [
        "withdraw", "withdrawal", "withdrawals", "withdrawing", "redeem", "redemption",
        "redemptions", "redeeming", "payout", "payouts", "take out my money",
        "get my money", "money back", "credited", "credit to my bank", "timeline", "timelines",
        "how long does it take", "when will i get", "exit load", "lock-in", "lock in",
        "settlement",
    ],
    Topic.ACCOUNT_CHANGES: [
        "nominee", "nominees", "nomination", "add a nominee", "change nominee",
        "account change", "account changes", "change of address", "address change",
        "change address", "update address", "update my address", "change my address",
        "bank account change", "change bank", "change my bank", "update bank",
        "bank details", "mobile number change", "change mobile", "update email",
        "change email", "email change", "update my details", "change my details",
        "change of name", "name change", "joint holder", "update profile",
    ],
}  # fmt: skip

_PATTERNS: dict[Topic, list[re.Pattern[str]]] = {
    topic: [
        re.compile(r"(?<![a-z0-9])" + re.escape(s).replace(r"\ ", r"\s+") + r"(?![a-z0-9])")
        for s in sorted(syns, key=len, reverse=True)
    ]
    for topic, syns in TOPIC_SYNONYMS.items()
}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}  # fmt: skip
_PICK_RE = re.compile(
    r"^(?:(?:option|number|no|topic|choice)\s*)?(\d|one|two|three|four|five)$"
    r"|^(?:the\s+)?(first|second|third|fourth|fifth|last)(?:\s+(?:one|option|topic))?$"
)


def _hits(text: str) -> dict[Topic, int]:
    """Number of distinct synonym spans per topic (longer synonyms consume their span first)."""
    counts: dict[Topic, int] = {}
    for topic, patterns in _PATTERNS.items():
        taken: list[tuple[int, int]] = []
        for p in patterns:
            for m in p.finditer(text):
                if not any(a < m.end() and m.start() < b for a, b in taken):
                    taken.append((m.start(), m.end()))
        if taken:
            counts[topic] = len(taken)
    return counts


def match_topic(text: str) -> tuple[Topic | None, list[Topic]]:
    """Return (topic, candidates).

    - one clear winner → (topic, [topic])
    - tie between several topics → (None, [tied topics...]) - ambiguous
    - nothing → (None, [])
    """
    t = text.lower()
    for topic in Topic:  # exact label (quick-reply chips send these)
        if t.strip() == topic.value.lower():
            return topic, [topic]
    counts = _hits(t)
    if not counts:
        return None, []
    best = max(counts.values())
    winners = [topic for topic in TOPIC_ORDER if counts.get(topic) == best]
    if len(winners) == 1:
        return winners[0], winners
    return None, winners


def pick_from_list(text: str) -> Topic | None:
    """'2', 'option 3', 'the first one' → topic by its position in the ASK_TOPIC list."""
    m = _PICK_RE.match(text.strip().lower())
    if m is None:
        return None
    token = m.group(1) or m.group(2)
    n = int(token) if token.isdigit() else _ORDINALS[token]
    return TOPIC_ORDER[n - 1] if 1 <= n <= len(TOPIC_ORDER) else None


class TopicMapper:
    def map(self, text: str) -> Topic | None:
        return match_topic(text)[0]

    def candidates(self, text: str) -> list[Topic]:
        return match_topic(text)[1]
