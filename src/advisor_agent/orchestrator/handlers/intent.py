"""INTENT_DETECT / CLARIFY handler (LLD 2.8, 6.5): routes the five intents, and answers the
yes/no follow-up a side flow left open ("Would you like to book a slot?")."""

from advisor_agent.domain.models import Intent
from advisor_agent.nlu.schema import NLUResult, YesNo
from advisor_agent.orchestrator.context import Flow, FollowUp, SessionContext
from advisor_agent.orchestrator.handlers.base import Deps, Outcome, followup_templates
from advisor_agent.orchestrator.handlers.booking import start_booking
from advisor_agent.orchestrator.handlers.secondary import (
    start_availability,
    start_code_flow,
    start_prepare,
)
from advisor_agent.orchestrator.preference import has_preference
from advisor_agent.orchestrator.states import State

CONFIDENCE_MIN = 0.7  # default; the live value is Deps.confidence_threshold
MAX_CLARIFY_FAILURES = 3
_ROUTED = {
    Intent.BOOK_NEW,
    Intent.RESCHEDULE,
    Intent.CANCEL,
    Intent.WHAT_TO_PREPARE,
    Intent.CHECK_AVAILABILITY,
}


async def _book(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    ctx.reset_retry(State.INTENT_DETECT)
    if ctx.flow is not Flow.BOOK:
        ctx.booking_code = None
        ctx.secure_url = None
        ctx.current_slot = None
        ctx.offered_slots = []
        ctx.chosen_slot = None
    ctx.flow = Flow.BOOK
    ctx.followup = None
    return await start_booking(ctx, nlu, deps)


async def handle_intent(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    threshold = deps.confidence_threshold
    confident = nlu.intent in _ROUTED and nlu.confidence >= threshold

    if ctx.followup is not None and not confident and nlu.yes_no is not None:
        followup, ctx.followup = ctx.followup, None
        if nlu.yes_no is YesNo.NO:
            return Outcome(State.CLOSE, ["GOODBYE"])
        if followup is FollowUp.BOOK:
            return await _book(ctx, nlu, deps)
        return Outcome(State.INTENT_DETECT, ["ASK_HOW_HELP"])

    implicit_book = nlu.intent in (None, Intent.UNKNOWN) and (
        nlu.topic is not None or bool(nlu.topic_candidates) or has_preference(nlu)
    )
    if implicit_book and ctx.flow is Flow.PREPARE and not has_preference(nlu):
        return await start_prepare(ctx, nlu, deps)  # "what about SIP?" after a prep guide
    if implicit_book or (confident and nlu.intent is Intent.BOOK_NEW):
        return await _book(ctx, nlu, deps)
    if confident:
        ctx.reset_retry(State.INTENT_DETECT)
        match nlu.intent:
            case Intent.RESCHEDULE:
                return await start_code_flow(ctx, nlu, deps, Flow.RESCHEDULE)
            case Intent.CANCEL:
                return await start_code_flow(ctx, nlu, deps, Flow.CANCEL)
            case Intent.WHAT_TO_PREPARE:
                return await start_prepare(ctx, nlu, deps)
            case _:
                return await start_availability(ctx, nlu, deps)
    if nlu.intent is Intent.SMALL_TALK:  # greetings/thanks are not misunderstandings
        if ctx.followup is not None:
            return Outcome(State.INTENT_DETECT, followup_templates(ctx))
        return Outcome(State.INTENT_DETECT, ["ASK_HOW_HELP"])

    if ctx.bump_retry(State.INTENT_DETECT) >= MAX_CLARIFY_FAILURES:
        return Outcome(State.CLOSE, ["FALLBACK_SECURE_LINK", "GOODBYE"])
    templates = ["CLARIFY_INTENT"]
    if deps.stub_hints:
        templates.append("STUB_HELP")
    return Outcome(State.CLARIFY, templates)
