import pytest

from advisor_agent.guardrails.pii import contains_pii, redact


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("my number is 9876543210", "phone"),
        ("call +91 98765 43210", "phone"),
        ("call +91-9876543210 please", "phone"),
        ("mail me at jane.doe@example.com", "email"),
        ("pay to jane@okhdfcbank", "upi"),
        ("PAN is ABCDE1234F", "pan"),
        ("pan abcde1234f", "pan"),
        ("aadhaar 1234 5678 9012", "aadhaar"),
        ("ifsc HDFC0001234", "ifsc"),
        ("card 4111 1111 1111 1111", "card"),
        ("account 123456789012345", "account"),
        ("nine eight seven six five four three two one zero", "spoken_digits"),
    ],
)
def test_detects(text, kind):
    redacted, hits = redact(text)
    assert [h.kind for h in hits] == [kind]
    assert f"[REDACTED:{kind}]" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "reschedule NL-A742",
        "cancel nl-a742",
        "time 2026-10-06 morning",
        "how about 2:30 PM",
        "time tuesday afternoon",
        "1",
        "option 2",
        "topic sip",
        "nine thirty please",
    ],
)
def test_allows_codes_dates_times_and_commands(text):
    assert redact(text) == (text, [])


def test_redaction_keeps_surrounding_text():
    redacted, _ = redact("ok jane@example.com and 9876543210 thanks")
    assert redacted == "ok [REDACTED:email] and [REDACTED:phone] thanks"


def test_card_requires_luhn():
    # 16 digits failing Luhn is still caught, but as an account number, not a card
    _, hits = redact("4111111111111112")
    assert [h.kind for h in hits] == ["account"]


def test_contains_pii():
    assert contains_pii("ABCDE1234F")
    assert not contains_pii("Tuesday, 6 October 2026, 2:00 PM IST")
