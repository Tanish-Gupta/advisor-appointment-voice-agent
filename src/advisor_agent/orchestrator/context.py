"""Per-session conversation state (LLD 2.2). Deliberately has no `channel` field."""

from enum import StrEnum

from pydantic import BaseModel

from advisor_agent.domain.models import Preference, Slot, Topic
from advisor_agent.nlu.schema import NLUResult
from advisor_agent.orchestrator.states import State


class Flow(StrEnum):
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    PREPARE = "prepare"
    AVAILABILITY = "availability"


class FollowUp(StrEnum):
    """What a bare yes/no answers at INTENT_DETECT after a side flow finished."""

    BOOK = "book"  # "Would you like to book a slot?" (prepare / availability / unknown code)
    ANYTHING_ELSE = "anything_else"  # "Is there anything else I can help with?"


class SessionContext(BaseModel):
    session_id: str
    state: State = State.START
    flow: Flow | None = None
    disclaimer_acknowledged: bool = False
    topic: Topic | None = None
    preference: Preference | None = None
    offered_slots: list[Slot] = []
    rejected_slot_ids: set[str] = set()
    chosen_slot: Slot | None = None
    booking_code: str | None = None
    secure_url: str | None = None
    retries: dict[State, int] = {}
    last_messages: list[str] = []
    turn_no: int = 0
    resume_state: State | None = None  # AWAIT_PIVOT: where the advice interrupt came from
    # Phase 5/6
    waitlist: bool = False  # EXECUTE should create a waitlist entry (no slot matched)
    current_slot: Slot | None = None  # reschedule/cancel: the booking's existing slot
    followup: FollowUp | None = None
    # A request made before the disclaimer was acknowledged; continued right after the "yes".
    pending_request: NLUResult | None = None
    # Weekend request -> the next working day we suggested; a plain "yes" accepts it.
    suggested_pref: Preference | None = None

    def bump_retry(self, state: State) -> int:
        self.retries[state] = self.retries.get(state, 0) + 1
        return self.retries[state]

    def reset_retry(self, state: State) -> None:
        self.retries.pop(state, None)
