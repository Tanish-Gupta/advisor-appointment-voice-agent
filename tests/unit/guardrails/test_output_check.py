import pytest

from advisor_agent.guardrails.output_check import OutputViolation, check, violations

FULL = "Tuesday, 6 October 2026, 2:00 PM IST"


def test_clean_messages_pass():
    msgs = [f"I have two options (IST): 1) {FULL} 2) Tuesday, 6 October 2026, 3:00 PM IST."]
    assert check(msgs, "offer_slots", strict=True) == msgs


def test_pii_in_output():
    assert violations(["Call us on 9876543210"], "collect_pref")


@pytest.mark.parametrize("phrase", ["You should invest in X", "I recommend this", "a good fund"])
def test_advice_phrasing(phrase):
    assert violations([phrase], "intent_detect")


@pytest.mark.parametrize("msg", ["How about 2:00 PM?", "Tuesday 2:00 PM IST", "6 October, 2:00 PM"])
def test_slot_without_full_date_or_ist(msg):
    assert violations([msg], "offer_slots")
    assert violations([msg], "confirm_slot")
    assert not violations([msg], "collect_pref")  # rule 3 only applies in slot states


def test_strict_raises_and_prod_returns_none():
    with pytest.raises(OutputViolation):
        check(["I recommend this"], "intent_detect", strict=True)
    assert check(["I recommend this"], "intent_detect", strict=False) is None


def test_secure_url_is_not_pii():
    assert not violations(["Finish here: https://example.com/complete?ref=NL-A742"], "close")


def test_close_repeats_full_ist_slot():
    assert not violations([f"Booked: {FULL}."], "close")
    assert violations(["Booked for 2:00 PM."], "close")


def test_code_on_close_requires_secure_link():
    assert violations(["Your booking code is NL-A742."], "close")
    assert not violations(
        ["Your booking code is NL-A742.", "Finish here: https://x.test/b/NL-A742?t=a.b"], "close"
    )


# --- Phase 5/6 additions -------------------------------------------------------------------


def test_requested_window_with_ist_is_allowed_on_close() -> None:
    from advisor_agent.guardrails.output_check import violations

    msg = "No free slots for Tuesday, 6 October 2026 (around 3:00 PM) (IST)."
    assert violations([msg], "close", ["NO_MATCH_WAITLIST"]) == []
    assert violations(["No free slots around 3:00 PM."], "close", []) != []


def test_code_without_link_only_flags_read_code() -> None:
    from advisor_agent.guardrails.output_check import violations

    msgs = ["Done — booking NL-A742 is cancelled."]
    assert violations(msgs, "close", ["CANCELLED", "GOODBYE"]) == []
    assert violations(msgs, "close", ["READ_CODE"]) != []
    assert violations(msgs, "close") != []  # unknown templates: strict


def test_cancel_confirm_is_a_slot_state() -> None:
    from advisor_agent.guardrails.output_check import violations

    assert violations(["Cancel the 3:00 PM slot?"], "cancel_confirm") != []
