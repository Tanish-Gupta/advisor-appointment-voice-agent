import re

import pytest

from advisor_agent.domain.codes import (
    CODE_RE,
    LETTERS,
    CodeSpaceExhausted,
    generate,
    parse_spoken_or_typed,
)
from advisor_agent.domain.models import BookingKind


def test_format_and_alphabet():
    for _ in range(500):
        code = generate(BookingKind.BOOKING, lambda c: False)
        assert CODE_RE.match(code)
        assert not re.search(r"[IOW01]", code[3:])


def test_alphabet_excludes_ambiguous_letters():
    assert not set("IOW") & set(LETTERS)
    assert len(LETTERS) == 23


def test_waitlist_prefix():
    for _ in range(100):
        assert generate(BookingKind.WAITLIST, lambda c: False).startswith("NL-W")


def test_uniqueness_with_set_backed_exists():
    seen: set[str] = set()
    for _ in range(5_000):
        code = generate(BookingKind.BOOKING, seen.__contains__)
        assert code not in seen
        seen.add(code)


def test_collision_retry():
    calls = {"n": 0}

    def exists(_code: str) -> bool:
        calls["n"] += 1
        return calls["n"] <= 5

    code = generate(BookingKind.BOOKING, exists)
    assert CODE_RE.match(code)
    assert calls["n"] == 6


def test_code_space_exhausted():
    with pytest.raises(CodeSpaceExhausted):
        generate(BookingKind.BOOKING, lambda c: True)


@pytest.mark.parametrize(
    "text",
    [
        "NL-A742",
        "nl a742",
        "my code is nl-a742 thanks",
        "N L A seven four two",
        "A as in apple 7 4 2",
        "N as in Nancy L as in Lima A as in Alpha seven four two",
    ],
)
def test_parse_spoken_or_typed(text):
    assert parse_spoken_or_typed(text) == "NL-A742"


@pytest.mark.parametrize("text", ["hello there", "NL-I742", "NL-A102", "book a slot", ""])
def test_parse_rejects_invalid(text):
    assert parse_spoken_or_typed(text) is None
