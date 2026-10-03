"""DISCLAIMER_ACK handler (LLD 2.5).

Conversational behaviour: a request made before the "yes" ("I'd like to book a KYC call") is
remembered and continued right after the acknowledgement. An opening request is eagerness, not a
refusal, so it does not count towards MAX_REFUSALS; every other non-"yes" reply does.
"""

from advisor_agent.domain.models import Intent
from advisor_agent.nlu.schema import NLUResult, YesNo
from advisor_agent.orchestrator.context import SessionContext
from advisor_agent.orchestrator.handlers.base import Deps, Outcome, enter
from advisor_agent.orchestrator.handlers.intent import handle_intent
from advisor_agent.orchestrator.preference import has_preference
from advisor_agent.orchestrator.states import State

MAX_REFUSALS = 2
_REQUESTS = {
    Intent.BOOK_NEW,
    Intent.RESCHEDULE,
    Intent.CANCEL,
    Intent.WHAT_TO_PREPARE,
    Intent.CHECK_AVAILABILITY,
}


def _is_request(nlu: NLUResult) -> bool:
    return (
        nlu.intent in _REQUESTS
        or nlu.topic is not None
        or bool(nlu.topic_candidates)
        or has_preference(nlu)
    )


def _merge(pending: NLUResult, now: NLUResult) -> NLUResult:
    """Fill gaps in the remembered request with anything new said alongside the "yes"."""
    update = {
        k: v
        for k, v in now.model_dump(exclude={"yes_no", "confidence", "source"}).items()
        if v not in (None, [], "")
    }
    if now.intent in (None, Intent.UNKNOWN, Intent.SMALL_TALK):
        update.pop("intent", None)
    return pending.model_copy(update=update)


async def handle_disclaimer(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.yes_no is YesNo.YES:
        ctx.disclaimer_acknowledged = True
        ctx.reset_retry(State.DISCLAIMER_ACK)
        request = nlu if _is_request(nlu) else None
        if ctx.pending_request is not None:
            request = _merge(ctx.pending_request, nlu)
        ctx.pending_request = None
        if request is None:
            return Outcome(State.INTENT_DETECT, ["ACK_THANKS", "ASK_HOW_HELP"])
        enter(ctx, State.INTENT_DETECT)
        outcome = await handle_intent(ctx, request.model_copy(update={"yes_no": None}), deps)
        return Outcome(outcome.next_state, ["ACK_THANKS", *outcome.templates], outcome.params)

    first_reply = not ctx.retries.get(State.DISCLAIMER_ACK) and ctx.pending_request is None
    if first_reply and nlu.yes_no is not YesNo.NO and _is_request(nlu):
        ctx.pending_request = nlu  # remember it; just need the quick "okay" first
        return Outcome(State.DISCLAIMER_ACK, ["DISCLAIMER_NEED_ACK"])
    if ctx.bump_retry(State.DISCLAIMER_ACK) >= MAX_REFUSALS:
        return Outcome(State.CLOSE, ["CANNOT_PROCEED_WITHOUT_ACK", "GOODBYE"])
    return Outcome(State.DISCLAIMER_ACK, ["DISCLAIMER", "DISCLAIMER_ASK_ACK"])
