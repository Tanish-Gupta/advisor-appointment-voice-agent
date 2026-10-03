"""Phase 6 subgraphs (LLD 6.1-6.4): reschedule / cancel (ASK_CODE, CANCEL_CONFIRM),
what_to_prepare (PREP_TOPIC) and check_availability (AVAILABILITY).

Nothing here asks for personal details: a booking is identified only by its code.
"""

import logging

from advisor_agent.domain.models import BookingKind, BookingStatus, Preference, TimeWindow
from advisor_agent.domain.timeutil import fmt_date, fmt_time, to_ist
from advisor_agent.nlu.schema import NLUResult, YesNo
from advisor_agent.orchestrator.content import prep_guides
from advisor_agent.orchestrator.context import Flow, FollowUp, SessionContext
from advisor_agent.orchestrator.handlers.base import Deps, Outcome, cancel_readback, enter
from advisor_agent.orchestrator.handlers.booking import (
    _PREF_ERROR_TEMPLATE,
    _preference,
    accept_suggestion,
    search_and_offer,
    weekend_reply,
)
from advisor_agent.orchestrator.preference import PreferenceError, has_preference
from advisor_agent.orchestrator.states import State

log = logging.getLogger(__name__)

MAX_CODE_ATTEMPTS = 2  # unknown code twice -> offer a new booking instead (LLD 6.1)
MAX_CODE_PROMPTS = 3  # no code heard at all
_AVAILABILITY_DAYS = 3  # working days listed when no day was given
_MOVABLE = {BookingStatus.TENTATIVE, BookingStatus.NEEDS_ATTENTION}


def _reset_flow(ctx: SessionContext, flow: Flow) -> None:
    ctx.flow = flow
    ctx.followup = None
    ctx.booking_code = None
    ctx.secure_url = None
    ctx.current_slot = None
    ctx.offered_slots = []
    ctx.chosen_slot = None
    ctx.rejected_slot_ids = set()
    ctx.waitlist = False


# --- reschedule / cancel ----------------------------------------------------------------------


async def start_code_flow(ctx: SessionContext, nlu: NLUResult, deps: Deps, flow: Flow) -> Outcome:
    _reset_flow(ctx, flow)
    if deps.executor is None:  # no booking store / MCP tools in this build
        ctx.followup = FollowUp.ANYTHING_ELSE
        return Outcome(State.INTENT_DETECT, ["CODE_LOOKUP_NOT_ENABLED", "ANYTHING_ELSE"])
    if nlu.booking_code:
        enter(ctx, State.ASK_CODE)
        return await handle_ask_code(ctx, nlu, deps)
    return Outcome(State.ASK_CODE, ["ASK_CODE"])


async def handle_ask_code(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    code = nlu.booking_code
    if code is None:
        if ctx.bump_retry(State.ASK_CODE) >= MAX_CODE_PROMPTS:
            return _give_up(ctx)
        return Outcome(State.ASK_CODE, ["ASK_CODE_REPROMPT"])
    if deps.executor is None:
        ctx.followup = FollowUp.ANYTHING_ELSE
        return Outcome(State.INTENT_DETECT, ["CODE_LOOKUP_NOT_ENABLED", "ANYTHING_ELSE"])

    booking = await deps.executor.lookup(code)
    if booking is None:
        if ctx.bump_retry(State.ASK_CODE) >= MAX_CODE_ATTEMPTS:
            return _give_up(ctx)
        return Outcome(State.ASK_CODE, ["CODE_NOT_FOUND"], {"code": code})
    ctx.reset_retry(State.ASK_CODE)
    if booking.status is BookingStatus.CANCELLED:
        ctx.followup = FollowUp.BOOK
        return Outcome(State.INTENT_DETECT, ["CODE_CANCELLED", "OFFER_TO_BOOK"], {"code": code})

    ctx.booking_code = code
    ctx.topic = booking.topic
    ctx.current_slot = booking.slot

    if ctx.flow is Flow.CANCEL:
        return Outcome(State.CANCEL_CONFIRM, cancel_readback(ctx))

    # Reschedule
    if booking.kind is BookingKind.WAITLIST or booking.slot is None:
        ctx.followup = FollowUp.BOOK
        ctx.booking_code = None
        return Outcome(State.INTENT_DETECT, ["WAITLIST_NO_RESCHEDULE", "OFFER_TO_BOOK"],
                       {"code": code})  # fmt: skip
    if booking.status not in _MOVABLE:
        ctx.followup = FollowUp.ANYTHING_ELSE
        ctx.booking_code = None
        return Outcome(State.INTENT_DETECT, ["CANNOT_RESCHEDULE", "ANYTHING_ELSE"],
                       {"code": code})  # fmt: skip
    if has_preference(nlu):
        return await search_and_offer(ctx, nlu, deps, prefix=["CODE_FOUND"])
    return Outcome(State.COLLECT_PREF, ["CODE_FOUND", "ASK_NEW_PREF"])


def _give_up(ctx: SessionContext) -> Outcome:
    ctx.reset_retry(State.ASK_CODE)
    ctx.followup = FollowUp.BOOK
    return Outcome(State.INTENT_DETECT, ["CODE_NOT_FOUND_GIVE_UP", "OFFER_TO_BOOK"])


async def handle_cancel_confirm(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.yes_no is YesNo.YES:
        return Outcome(State.EXECUTE, [])  # the orchestrator runs handle_execute next
    if nlu.yes_no is YesNo.NO:
        ctx.followup = FollowUp.ANYTHING_ELSE
        return Outcome(State.INTENT_DETECT, ["CANCEL_ABORTED", "ANYTHING_ELSE"])
    return Outcome(State.CANCEL_CONFIRM, ["CONFIRM_REPROMPT", *cancel_readback(ctx)])


# --- what_to_prepare ----------------------------------------------------------------------------


def _guide(ctx: SessionContext) -> Outcome:
    assert ctx.topic is not None
    ctx.followup = FollowUp.BOOK
    return Outcome(
        State.INTENT_DETECT,
        ["PREP_GUIDE", "OFFER_TO_BOOK"],
        {"guide": prep_guides()[ctx.topic]},
    )


async def start_prepare(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    _reset_flow(ctx, Flow.PREPARE)
    if nlu.topic is not None:
        ctx.topic = nlu.topic
        return _guide(ctx)
    if nlu.topic_candidates:
        return Outcome(State.PREP_TOPIC, ["TOPIC_OPTIONS"])
    return Outcome(State.PREP_TOPIC, ["PREP_ASK_TOPIC"])


async def handle_prep_topic(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.topic is None:
        ctx.bump_retry(State.PREP_TOPIC)
        return Outcome(State.PREP_TOPIC, ["TOPIC_OPTIONS"])
    ctx.reset_retry(State.PREP_TOPIC)
    ctx.topic = nlu.topic
    return _guide(ctx)


# --- check_availability -------------------------------------------------------------------------


def _clip(windows: list[TimeWindow], pref_window: TimeWindow | None) -> list[str]:
    labels = []
    for w in windows:
        start, end = w.start, w.end
        if pref_window is not None:
            start, end = max(start, pref_window.start), min(end, pref_window.end)
            if start >= end:
                continue
        labels.append(f"{fmt_time(start)} to {fmt_time(end)}")
    return labels


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def availability_text(deps: Deps, pref: Preference) -> str | None:
    """'Tuesday, 6 October 2026: 9:00 AM to 12:00 PM and 2:00 PM to 6:00 PM; ...' (IST).

    Peek mode (LLD 6.4): reads the mock calendar only - no MCP calls, no holds.
    """
    today = to_ist(deps.clock()).date()
    if pref.date is not None:
        by_day = deps.picker.availability_windows(pref.date, 1)
        limit = 1
    else:
        by_day = deps.picker.availability_windows(today, deps.horizon_days)
        limit = _AVAILABILITY_DAYS
    days: list[str] = []
    for day in sorted(by_day):
        labels = _clip(by_day[day], pref.window)
        if labels:
            days.append(f"{fmt_date(day)}: {_join(labels)}")
        if len(days) >= limit:
            break
    return "; ".join(days) or None


async def start_availability(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    _reset_flow(ctx, Flow.AVAILABILITY)
    if nlu.topic is not None:
        ctx.topic = nlu.topic
    return await _peek(ctx, nlu, deps)


async def handle_availability(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.topic is not None:
        ctx.topic = nlu.topic
    suggested = accept_suggestion(ctx, nlu)
    if suggested is not None:
        return await _peek(ctx, nlu, deps, suggested)
    if not has_preference(nlu):
        if nlu.yes_no is YesNo.NO:
            return Outcome(State.CLOSE, ["GOODBYE"])
        return Outcome(State.AVAILABILITY, ["ASK_DAY_FOR_AVAILABILITY"])
    return await _peek(ctx, nlu, deps)


async def _peek(
    ctx: SessionContext, nlu: NLUResult, deps: Deps, pref: Preference | None = None
) -> Outcome:
    explicit = pref is not None or has_preference(nlu)
    if pref is None:
        pref = Preference()
        if has_preference(nlu):
            try:
                pref = _preference(nlu, deps)
            except PreferenceError as e:
                if e.reason == "non_working_day":
                    return weekend_reply(ctx, nlu, deps, State.AVAILABILITY, [])
                return Outcome(State.AVAILABILITY, [_PREF_ERROR_TEMPLATE[e.reason]])
    text = availability_text(deps, pref)
    if text is None:
        ctx.preference = pref
        return Outcome(State.AVAILABILITY, ["AVAILABILITY_NONE"])
    if explicit:
        ctx.preference = pref  # remembered for "yes, book it"
    ctx.followup = FollowUp.BOOK
    return Outcome(State.INTENT_DETECT, ["AVAILABILITY_LIST", "OFFER_TO_BOOK"], {"windows": text})
