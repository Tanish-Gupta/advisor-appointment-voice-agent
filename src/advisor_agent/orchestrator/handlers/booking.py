"""Booking subgraph handlers: TOPIC_CONFIRM, COLLECT_PREF, OFFER_SLOTS, CONFIRM_SLOT and
EXECUTE (LLD 2.8, 3.12). Shared by book_new, the waitlist branch (Phase 5) and reschedule /
cancel (Phase 6): EXECUTE dispatches on the flow."""

import logging
import re
from collections.abc import Iterable
from datetime import date, time, timedelta

from advisor_agent.domain.models import Preference, Slot
from advisor_agent.domain.slots import SlotTaken
from advisor_agent.domain.timeutil import fmt_date, fmt_slot, fmt_time, to_ist
from advisor_agent.nlu.date_resolver import ANY, extract_time
from advisor_agent.nlu.schema import NLUResult, YesNo
from advisor_agent.orchestrator.context import Flow, SessionContext
from advisor_agent.orchestrator.handlers.base import (
    Deps,
    Outcome,
    confirm_templates,
    enter,
    offer_templates,
)
from advisor_agent.orchestrator.preference import (
    PreferenceError,
    has_preference,
    preference_from_nlu,
)
from advisor_agent.orchestrator.states import State

log = logging.getLogger(__name__)

_BUSY_ROUNDS = 3
_PREF_ERROR_TEMPLATE = {
    "out_of_range": "PREF_OUT_OF_RANGE",
    "non_working_day": "PREF_NON_WORKING_DAY",
    "outside_hours": "PREF_OUTSIDE_HOURS",
}


def _preference(nlu: NLUResult, deps: Deps) -> Preference:
    today = to_ist(deps.clock()).date()
    return preference_from_nlu(nlu, today, deps.horizon_days, deps.business_hours)


def _carry_over(prev: Preference | None, new: Preference, nlu: NLUResult) -> Preference:
    """'what about Wednesday?' keeps the earlier time window; 'mornings instead' keeps the day.

    Not applied when the user explicitly said they have no day preference ('any day').
    """
    if prev is None or nlu.date_text == ANY:
        return new
    said_day = nlu.date_text is not None or nlu.date_iso is not None
    said_time = nlu.time_text is not None  # includes "any time" (window None)
    if said_day and not said_time:
        return Preference(date=new.date, window=prev.window)
    if said_time and not said_day:
        return Preference(date=prev.date, window=new.window)
    return new


async def start_booking(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    """Entry into the book flow; skips steps whose answers are already present."""
    if nlu.topic is not None:
        ctx.topic = nlu.topic
    if ctx.topic is None:
        if has_preference(nlu):  # remember "book time tuesday afternoon" for after the topic
            try:
                ctx.preference = _preference(nlu, deps)
            except PreferenceError:
                pass
        # Several topics mentioned ("tax on my SIP withdrawal"): ask them to pick one.
        ask = "TOPIC_OPTIONS" if nlu.topic_candidates else "ASK_TOPIC"
        return Outcome(State.TOPIC_CONFIRM, [ask])
    if has_preference(nlu):
        return await search_and_offer(ctx, nlu, deps, prefix=["TOPIC_ACK"])
    if ctx.preference is not None:  # e.g. remembered from check_availability
        ctx.rejected_slot_ids = set()
        pref = ctx.preference
        return await _offer(ctx, await pick_available(ctx, deps, pref), ["TOPIC_ACK"], deps)
    return Outcome(State.COLLECT_PREF, ["TOPIC_ACK", "ASK_PREF"])


async def pick_available(
    ctx: SessionContext, deps: Deps, pref: Preference, exclude: Iterable[str] = frozenset()
) -> list[Slot]:
    """SlotPicker.pick_two, minus slots that are busy on the real advisor calendar.

    With MCP tools enabled, the candidates are checked with `calendar_list_busy` (read-only,
    gated) and busy ones are replaced, up to three rounds. Fails open: if the calendar cannot
    be read, the mock-calendar slots are offered and EXECUTE re-checks before booking.
    """
    excluded = set(exclude)
    if ctx.current_slot is not None:  # reschedule: never offer the slot being moved
        excluded.add(ctx.current_slot.slot_id)
    slots = deps.picker.pick_two(pref, exclude=excluded)
    if deps.executor is None or not slots:
        return slots
    for attempt in range(_BUSY_ROUNDS):
        try:
            busy = await deps.executor.busy_slot_ids(slots, ctx.state)
        except Exception as e:
            log.warning("busy check failed; offering unchecked slots (%s)", type(e).__name__)
            return slots
        if not busy:
            return slots
        excluded |= busy
        if attempt == _BUSY_ROUNDS - 1:
            return [s for s in slots if s.slot_id not in busy]
        slots = deps.picker.pick_two(pref, exclude=excluded)
        if not slots:
            return []
    return slots


async def _offer(ctx: SessionContext, slots: list[Slot], prefix: list[str], deps: Deps) -> Outcome:
    ctx.offered_slots = slots
    ctx.chosen_slot = None
    if slots:
        return Outcome(State.OFFER_SLOTS, [*prefix, *offer_templates(slots)])
    if (
        ctx.flow is Flow.BOOK
        and deps.executor is not None
        and ctx.topic is not None
        and ctx.preference is not None
    ):
        # Phase 5 waitlist branch (LLD 5.1): COLLECT_PREF -> EXECUTE (waitlist) -> CLOSE.
        enter(ctx, State.COLLECT_PREF)
        ctx.waitlist = True
        return Outcome(State.EXECUTE, prefix)
    # No MCP tools, or a reschedule: ask for another day instead (no waitlist for a move).
    return Outcome(State.COLLECT_PREF, [*prefix, "NO_MATCH_TRY_OTHER"])


def _exact_time(nlu: NLUResult) -> time | None:
    """'2pm' / 'at 14:30' -> 14:00 / 14:30; windows like 'afternoon' or 'after 4' -> None."""
    text = (nlu.time_text or "").strip().lower()
    canonical = text if text.startswith("around ") else extract_time(text) if text else None
    if canonical is None or not canonical.startswith("around "):
        return None
    try:
        return time.fromisoformat(canonical.split(" ", 1)[1])
    except ValueError:
        return None


_AROUND = re.compile(r"^around (\d{1,2}):(\d{2}) (AM|PM)$")


def _exact_from_window(pref: Preference) -> time | None:
    """'2pm' becomes the window labelled 'around 2:00 PM'; recover the 2:00 PM."""
    m = _AROUND.match(pref.window.label) if pref.window else None
    if m is None:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
    return time(hour, int(m.group(2)))


def _next_working_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def weekend_reply(
    ctx: SessionContext, nlu: NLUResult, deps: Deps, state: State, prefix: list[str]
) -> Outcome:
    """'Saturday 2pm': explain advisors work weekdays and suggest the next working day with the
    same time window (a plain "yes" then accepts it - see accept_suggestion)."""
    ctx.suggested_pref = None
    if nlu.date_iso is not None:
        moved = nlu.model_copy(update={"date_iso": _next_working_day(nlu.date_iso)})
        try:
            ctx.suggested_pref = _carry_over(ctx.preference, _preference(moved, deps), moved)
        except PreferenceError:
            pass
    if ctx.suggested_pref is None or nlu.date_iso is None:
        return Outcome(state, [*prefix, "PREF_WEEKDAYS_ONLY"])
    return Outcome(state, [*prefix, "PREF_NON_WORKING_DAY"], {"asked_day": fmt_date(nlu.date_iso)})


def accept_suggestion(ctx: SessionContext, nlu: NLUResult) -> Preference | None:
    """The suggested weekday after a weekend request, if this turn is a plain "yes"."""
    pref = ctx.suggested_pref
    if pref is None or nlu.yes_no is not YesNo.YES or has_preference(nlu):
        return None
    ctx.suggested_pref = None
    return pref


async def search_and_offer(
    ctx: SessionContext, nlu: NLUResult, deps: Deps, prefix: list[str] | None = None
) -> Outcome:
    prefix = list(prefix or [])
    try:
        pref = _carry_over(ctx.preference, _preference(nlu, deps), nlu)
    except PreferenceError as e:
        ctx.offered_slots = []
        if e.reason == "non_working_day":
            return weekend_reply(ctx, nlu, deps, State.COLLECT_PREF, prefix)
        return Outcome(State.COLLECT_PREF, [*prefix, _PREF_ERROR_TEMPLATE[e.reason]])
    return await offer_for(ctx, pref, deps, prefix, exact=_exact_time(nlu))


async def offer_for(
    ctx: SessionContext,
    pref: Preference,
    deps: Deps,
    prefix: list[str] | None = None,
    exact: time | None = None,
) -> Outcome:
    prefix = list(prefix or [])
    params: dict[str, str] = {}
    ctx.suggested_pref = None
    ctx.preference = pref
    ctx.rejected_slot_ids = set()
    slots = await pick_available(ctx, deps, pref)
    if slots and exact is not None and pref.date is not None:
        if not any(to_ist(s.start_utc).time() == exact for s in slots):
            # "5th Oct 2pm" when 2 PM is booked: say so instead of silently offering 1:00/1:30.
            prefix.append("EXACT_TIME_TAKEN")
            params["requested"] = f"{fmt_date(pref.date)}, {fmt_time(exact)} IST"
    outcome = await _offer(ctx, slots, prefix, deps)
    outcome.params = {**params, **outcome.params}
    return outcome


def _slot_at(nlu: NLUResult, offered: list[Slot]) -> int | None:
    """'1 pm works' / 'Monday 1:30' while two slots are on the table -> that slot's number."""
    exact = _exact_time(nlu)
    if exact is None:
        return None
    for i, s in enumerate(offered, start=1):
        start = to_ist(s.start_utc)
        if start.time() == exact and nlu.date_iso in (None, start.date()):
            return i
    return None


async def handle_topic(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.topic is None:
        ctx.bump_retry(State.TOPIC_CONFIRM)
        return Outcome(State.TOPIC_CONFIRM, ["TOPIC_OPTIONS"])
    ctx.topic = nlu.topic
    ctx.reset_retry(State.TOPIC_CONFIRM)
    if has_preference(nlu):
        return await search_and_offer(ctx, nlu, deps, prefix=["TOPIC_ACK"])
    if ctx.preference is not None:
        ctx.rejected_slot_ids = set()
        pref = ctx.preference
        return await _offer(ctx, await pick_available(ctx, deps, pref), ["TOPIC_ACK"], deps)
    return Outcome(State.COLLECT_PREF, ["TOPIC_ACK", "ASK_PREF"])


async def handle_collect_pref(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.topic is not None:
        ctx.topic = nlu.topic
    if has_preference(nlu):
        ctx.reset_retry(State.COLLECT_PREF)
        return await search_and_offer(ctx, nlu, deps)
    suggested = accept_suggestion(ctx, nlu)
    if suggested is not None:
        return await offer_for(ctx, suggested, deps, exact=_exact_from_window(suggested))
    ctx.bump_retry(State.COLLECT_PREF)
    return Outcome(State.COLLECT_PREF, ["PREF_UNCLEAR"])


async def handle_offer(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    offered = ctx.offered_slots
    choice = nlu.slot_choice
    if choice is None and nlu.yes_no is YesNo.YES and len(offered) == 1:
        choice = 1
    if choice is None:
        choice = _slot_at(nlu, offered)
    if choice is not None:
        if choice > len(offered):
            return Outcome(State.OFFER_SLOTS, offer_templates(offered))
        ctx.chosen_slot = offered[choice - 1]
        return Outcome(State.CONFIRM_SLOT, confirm_templates(ctx))

    if has_preference(nlu):  # "no, Wednesday instead" is a new preference, not a plain rejection
        return await search_and_offer(ctx, nlu, deps)

    if nlu.yes_no is YesNo.NO:
        ctx.rejected_slot_ids |= {s.slot_id for s in offered}
        slots = await pick_available(
            ctx, deps, ctx.preference or Preference(), ctx.rejected_slot_ids
        )
        if slots:
            ctx.offered_slots = slots
            return Outcome(State.OFFER_SLOTS, offer_templates(slots))
        ctx.offered_slots = []
        return Outcome(State.COLLECT_PREF, ["ASK_OTHER_PREF"])

    return Outcome(State.OFFER_SLOTS, ["OFFER_REPROMPT"])


async def handle_confirm(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    if nlu.yes_no is YesNo.YES:
        if deps.executor is None:  # MCP tools disabled (AGENT_MCP_TOOLS_ENABLED=false)
            return Outcome(State.CONFIRM_SLOT, ["EXECUTION_NOT_ENABLED"])
        return Outcome(State.EXECUTE, [])  # the orchestrator runs handle_execute next
    if nlu.yes_no is YesNo.NO:
        ctx.chosen_slot = None
        return Outcome(State.OFFER_SLOTS, offer_templates(ctx.offered_slots))
    return Outcome(State.CONFIRM_SLOT, ["CONFIRM_REPROMPT"])


async def handle_execute(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    """EXECUTE (LLD 3.12 / 4.1 / 5.1 / 6.1 / 6.2): run the flow's side effects through the
    gated MCP tools, then read back the result."""
    if ctx.waitlist:
        return await _execute_waitlist(ctx, deps)
    if ctx.flow is Flow.CANCEL:
        return await _execute_cancel(ctx, deps)
    if deps.executor is None or ctx.topic is None or ctx.chosen_slot is None:
        return Outcome(State.OFFER_SLOTS, offer_templates(ctx.offered_slots))
    slot = ctx.chosen_slot
    try:
        if ctx.flow is Flow.RESCHEDULE and ctx.booking_code:
            result = await deps.executor.reschedule(ctx.booking_code, slot)
        else:
            result = await deps.executor.book(ctx.session_id, ctx.topic, slot)
    except SlotTaken:
        ctx.chosen_slot = None
        ctx.rejected_slot_ids |= {slot.slot_id}
        slots = await pick_available(
            ctx, deps, ctx.preference or Preference(), ctx.rejected_slot_ids
        )
        ctx.offered_slots = slots
        if slots:
            return Outcome(State.OFFER_SLOTS, ["SLOT_JUST_TAKEN", *offer_templates(slots)])
        return Outcome(State.COLLECT_PREF, ["SLOT_JUST_TAKEN", "ASK_OTHER_PREF"])
    except Exception as e:
        log.warning("booking failed: %s", type(e).__name__, exc_info=True)
        retry = "CHANGE_RETRY" if ctx.flow is Flow.RESCHEDULE else "TOOL_RETRY"
        return Outcome(State.CONFIRM_SLOT, [retry])
    ctx.booking_code = result.code
    ctx.secure_url = result.secure_url
    done = "RESCHEDULED" if ctx.flow is Flow.RESCHEDULE else "BOOKED"
    return Outcome(
        State.CLOSE,
        [done, "READ_CODE", "SECURE_LINK", "GOODBYE"],
        {"slot": fmt_slot(slot), "ttl": result.ttl_label},
    )


async def _execute_waitlist(ctx: SessionContext, deps: Deps) -> Outcome:
    ctx.waitlist = False
    if deps.executor is None or ctx.topic is None or ctx.preference is None:
        return Outcome(State.COLLECT_PREF, ["NO_MATCH_TRY_OTHER"])
    try:
        result = await deps.executor.waitlist(ctx.session_id, ctx.topic, ctx.preference)
    except Exception as e:
        log.warning("waitlist failed: %s", type(e).__name__, exc_info=True)
        return Outcome(State.COLLECT_PREF, ["WAITLIST_FAILED"])
    ctx.booking_code = result.code
    ctx.secure_url = result.secure_url
    return Outcome(
        State.CLOSE,
        ["NO_MATCH_WAITLIST", "READ_WAITLIST_CODE", "SECURE_LINK", "GOODBYE"],
        {"ttl": result.ttl_label},
    )


async def _execute_cancel(ctx: SessionContext, deps: Deps) -> Outcome:
    if deps.executor is None or not ctx.booking_code:
        return Outcome(State.CANCEL_CONFIRM, ["CANCEL_RETRY"])
    try:
        await deps.executor.cancel(ctx.booking_code)
    except Exception as e:
        log.warning("cancel failed: %s", type(e).__name__, exc_info=True)
        return Outcome(State.CANCEL_CONFIRM, ["CANCEL_RETRY"])
    return Outcome(State.CLOSE, ["CANCELLED", "GOODBYE"])
