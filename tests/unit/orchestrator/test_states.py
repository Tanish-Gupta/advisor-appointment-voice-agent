import pytest

from advisor_agent.orchestrator.states import (
    ALLOWED_TRANSITIONS,
    State,
    TransitionNotAllowed,
    assert_transition_allowed,
    is_allowed,
)


def test_every_state_has_a_row():
    assert set(ALLOWED_TRANSITIONS) == set(State)


def test_start_only_goes_to_disclaimer():
    assert is_allowed(State.START, State.DISCLAIMER_ACK, disclaimer_acknowledged=False)
    for s in State:
        if s is not State.DISCLAIMER_ACK:
            assert not is_allowed(State.START, s, disclaimer_acknowledged=True)


@pytest.mark.parametrize("current", list(State))
@pytest.mark.parametrize("nxt", list(State))
def test_nothing_past_disclaimer_without_ack(current, nxt):
    if current is State.START:
        return
    allowed = is_allowed(current, nxt, disclaimer_acknowledged=False)
    assert allowed == (nxt in {State.DISCLAIMER_ACK, State.CLOSE})


def test_close_is_terminal():
    for s in State:
        assert is_allowed(State.CLOSE, s, disclaimer_acknowledged=True) == (s is State.CLOSE)


@pytest.mark.parametrize(
    ("current", "nxt"),
    [
        (State.DISCLAIMER_ACK, State.INTENT_DETECT),
        (State.INTENT_DETECT, State.TOPIC_CONFIRM),
        (State.INTENT_DETECT, State.CLARIFY),
        (State.TOPIC_CONFIRM, State.COLLECT_PREF),
        (State.COLLECT_PREF, State.OFFER_SLOTS),
        (State.OFFER_SLOTS, State.CONFIRM_SLOT),
        (State.CONFIRM_SLOT, State.OFFER_SLOTS),
        (State.OFFER_SLOTS, State.COLLECT_PREF),
    ],
)
def test_phase2_rows_allowed(current, nxt):
    assert_transition_allowed(current, nxt, disclaimer_acknowledged=True)


@pytest.mark.parametrize(
    ("current", "nxt"),
    [
        (State.COLLECT_PREF, State.CONFIRM_SLOT),  # must pick a slot first
        (State.TOPIC_CONFIRM, State.CONFIRM_SLOT),
        (State.INTENT_DETECT, State.EXECUTE),  # booking only via CONFIRM_SLOT/CANCEL_CONFIRM
        (State.OFFER_SLOTS, State.EXECUTE),
    ],
)
def test_forbidden(current, nxt):
    with pytest.raises(TransitionNotAllowed):
        assert_transition_allowed(current, nxt, disclaimer_acknowledged=True)
