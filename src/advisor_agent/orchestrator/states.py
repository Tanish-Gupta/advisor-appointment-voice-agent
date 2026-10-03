"""FSM states and allowed transitions (LLD 2.1, 2.8)."""

from enum import StrEnum


class State(StrEnum):
    START = "start"
    DISCLAIMER_ACK = "disclaimer_ack"
    INTENT_DETECT = "intent_detect"
    CLARIFY = "clarify"
    TOPIC_CONFIRM = "topic_confirm"
    COLLECT_PREF = "collect_pref"
    OFFER_SLOTS = "offer_slots"
    CONFIRM_SLOT = "confirm_slot"
    ASK_CODE = "ask_code"
    CANCEL_CONFIRM = "cancel_confirm"
    PREP_TOPIC = "prep_topic"
    AVAILABILITY = "availability"
    AWAIT_PIVOT = "await_pivot"
    EXECUTE = "execute"
    CLOSE = "close"


S = State
_FLOW_ENTRY = {S.TOPIC_CONFIRM, S.COLLECT_PREF, S.OFFER_SLOTS, S.ASK_CODE, S.PREP_TOPIC,
               S.AVAILABILITY}  # fmt: skip

# Same-state transitions and transitions to CLOSE / AWAIT_PIVOT are always allowed
# (re-prompts, stop, advice interrupt) and are not listed here.
ALLOWED_TRANSITIONS: dict[State, frozenset[State]] = {
    S.START: frozenset({S.DISCLAIMER_ACK}),
    S.DISCLAIMER_ACK: frozenset({S.INTENT_DETECT, *_FLOW_ENTRY}),
    S.INTENT_DETECT: frozenset({S.CLARIFY, *_FLOW_ENTRY}),
    S.CLARIFY: frozenset({S.INTENT_DETECT, *_FLOW_ENTRY}),
    S.TOPIC_CONFIRM: frozenset({S.COLLECT_PREF, S.OFFER_SLOTS}),
    S.COLLECT_PREF: frozenset({S.OFFER_SLOTS, S.EXECUTE}),
    S.OFFER_SLOTS: frozenset({S.CONFIRM_SLOT, S.COLLECT_PREF}),
    S.CONFIRM_SLOT: frozenset({S.OFFER_SLOTS, S.EXECUTE}),
    S.ASK_CODE: frozenset({S.INTENT_DETECT, S.COLLECT_PREF, S.OFFER_SLOTS, S.CANCEL_CONFIRM}),
    S.CANCEL_CONFIRM: frozenset({S.INTENT_DETECT, S.EXECUTE}),
    S.PREP_TOPIC: frozenset({S.INTENT_DETECT}),
    S.AVAILABILITY: frozenset({S.INTENT_DETECT}),
    S.AWAIT_PIVOT: frozenset(
        {S.INTENT_DETECT, S.CLARIFY, *_FLOW_ENTRY, S.CONFIRM_SLOT, S.CANCEL_CONFIRM}
    ),
    S.EXECUTE: frozenset({S.OFFER_SLOTS, S.CONFIRM_SLOT, S.COLLECT_PREF, S.CANCEL_CONFIRM}),
    S.CLOSE: frozenset(),
}

_ALWAYS = frozenset({S.CLOSE, S.AWAIT_PIVOT})


class TransitionNotAllowed(Exception):
    pass


def is_allowed(current: State, nxt: State, *, disclaimer_acknowledged: bool) -> bool:
    if current is S.START:
        return nxt is S.DISCLAIMER_ACK
    if not disclaimer_acknowledged:
        # Policy invariant: nothing but DISCLAIMER_ACK / CLOSE is reachable before the ack.
        return nxt in {S.DISCLAIMER_ACK, S.CLOSE}
    if current is S.CLOSE:
        return nxt is S.CLOSE
    if nxt is current or nxt in _ALWAYS:
        return True
    return nxt in ALLOWED_TRANSITIONS[current]


def assert_transition_allowed(current: State, nxt: State, *, disclaimer_acknowledged: bool) -> None:
    if not is_allowed(current, nxt, disclaimer_acknowledged=disclaimer_acknowledged):
        raise TransitionNotAllowed(
            f"{current} -> {nxt} (disclaimer_acknowledged={disclaimer_acknowledged})"
        )
