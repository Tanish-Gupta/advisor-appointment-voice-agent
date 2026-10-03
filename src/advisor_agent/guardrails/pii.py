"""PII gate (LLD 2.6). Regex-based detection and redaction of personal data in user text."""

import re
from dataclasses import dataclass

_NUM_WORDS = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|double|triple)"

# Order matters: each kind is replaced before the next runs, so later, broader patterns
# (account numbers) never re-match text already claimed by a narrower one (phone, card).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("upi", re.compile(r"[\w.-]+@(?:ok\w+|ybl|upi|paytm|ibl|axl)\b", re.IGNORECASE)),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)),
    ("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", re.IGNORECASE)),
    ("card", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("aadhaar", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("phone", re.compile(r"(?:\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")),
    ("account", re.compile(r"\b\d{9,18}\b")),
    ("spoken_digits", re.compile(rf"\b{_NUM_WORDS}(?:[\s-]+{_NUM_WORDS}){{6,}}\b", re.IGNORECASE)),
]

# Allow-list: booking codes, dates and times are never redacted.
_ALLOW = re.compile(
    r"\bNL-?[A-Z]\d{3}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}:\d{2}(?:\s?[AP]M)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PIIHit:
    kind: str  # phone | email | pan | aadhaar | account | card | ifsc | upi | spoken_digits
    span: tuple[int, int]  # span in the original text


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[PIIHit]]:
    """Return text with each hit replaced by '[REDACTED:<kind>]', plus the list of hits."""
    # Work on a same-length copy where allow-listed spans are masked, so spans stay aligned.
    masked = _ALLOW.sub(lambda m: "\u2063" * len(m.group(0)), text)
    claimed = [False] * len(text)
    hits: list[PIIHit] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(masked):
            start, end = m.span()
            if any(claimed[start:end]):
                continue
            if kind == "card" and not _luhn_ok(re.sub(r"\D", "", m.group(0))):
                continue
            hits.append(PIIHit(kind, (start, end)))
            for i in range(start, end):
                claimed[i] = True
    if not hits:
        return text, []
    hits.sort(key=lambda h: h.span[0])
    out, pos = [], 0
    for h in hits:
        out.append(text[pos : h.span[0]])
        out.append(f"[REDACTED:{h.kind}]")
        pos = h.span[1]
    out.append(text[pos:])
    return "".join(out), hits


def contains_pii(text: str) -> bool:
    return bool(redact(text)[1])
