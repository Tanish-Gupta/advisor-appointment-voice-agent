"""Booking codes (LLD 1.4). generate() is the plan's BookingCodeGenerator."""

import re
import secrets
from collections.abc import Callable

from advisor_agent.domain.models import BookingKind

LETTERS = "ABCDEFGHJKLMNPQRSTUVXYZ"  # no I, O, W (W reserved for waitlist)
DIGITS = "23456789"  # no 0, 1
CODE_RE = re.compile(r"^NL-([A-HJ-NP-Z])([2-9]{3})$")
MAX_ATTEMPTS = 20


class CodeSpaceExhausted(Exception):
    pass


def generate(kind: BookingKind, exists: Callable[[str], bool]) -> str:
    """Return a new unique code such as 'NL-A742' (or 'NL-W315' for the waitlist).

    Uniqueness is checked through `exists` (a set in tests, the bookings table from Phase 4).
    """
    for _ in range(MAX_ATTEMPTS):
        letter = "W" if kind is BookingKind.WAITLIST else secrets.choice(LETTERS)
        code = f"NL-{letter}{''.join(secrets.choice(DIGITS) for _ in range(3))}"
        if not exists(code):
            return code
    raise CodeSpaceExhausted


_TYPED_RE = re.compile(r"\bnl\s*-?\s*([a-z])\s*-?\s*(\d)\s*(\d)\s*(\d)\b", re.IGNORECASE)
_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def _canonical(letter: str, digits: str) -> str | None:
    code = f"NL-{letter.upper()}{digits}"
    return code if CODE_RE.match(code) else None


def parse_spoken_or_typed(text: str) -> str | None:
    """Accepts 'NL-A742', 'nl a742', 'N L A seven four two', 'A as in apple 7 4 2'.

    Returns the canonical code or None.
    """
    m = _TYPED_RE.search(text)
    if m:
        return _canonical(m.group(1), "".join(m.group(2, 3, 4)))

    tokens = re.findall(r"[a-z]+|\d", text.lower())
    runs: list[str] = []
    current = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) == 1 and tok.isalpha() and tokens[i + 1 : i + 3] == ["as", "in"]:
            current += tok  # "A as in apple" -> "a"
            i += 4
            continue
        if tok in _NUMBER_WORDS:
            current += _NUMBER_WORDS[tok]
        elif tok.isdigit() or (len(tok) == 1 and tok.isalpha()) or tok == "nl":
            current += tok
        else:
            if current:
                runs.append(current)
            current = ""
        i += 1
    if current:
        runs.append(current)

    for run in runs:
        m = re.search(r"nl([a-z])(\d{3})$", run) or re.fullmatch(r"([a-z])(\d{3})", run)
        if m:
            code = _canonical(m.group(1), m.group(2))
            if code:
                return code
    return None
