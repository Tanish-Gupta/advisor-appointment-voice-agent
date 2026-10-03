"""Turn pipeline (LLD 2.3): Orchestrator.handle_turn(session_id, user_text) -> TurnResult.

The orchestrator never knows whether text came from a keyboard or (Phase 7) speech-to-text.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from advisor_agent.domain.models import Intent
from advisor_agent.guardrails import output_check, pii
from advisor_agent.guardrails.advice import detect_advice
from advisor_agent.guardrails.output_check import OutputViolation
from advisor_agent.nlu.engine import NluEngine
from advisor_agent.nlu.rules import yes_no as rules_yes_no
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo
from advisor_agent.orchestrator import templates
from advisor_agent.orchestrator.audit import AuditEvent, AuditSink
from advisor_agent.orchestrator.content import edu_links_text
from advisor_agent.orchestrator.context import Flow, SessionContext
from advisor_agent.orchestrator.handlers.base import Deps, Handler, Outcome, enter, reprompt
from advisor_agent.orchestrator.handlers.booking import (
    handle_collect_pref,
    handle_confirm,
    handle_execute,
    handle_offer,
    handle_topic,
    start_booking,
)
from advisor_agent.orchestrator.handlers.disclaimer import handle_disclaimer
from advisor_agent.orchestrator.handlers.intent import handle_intent
from advisor_agent.orchestrator.handlers.secondary import (
    handle_ask_code,
    handle_availability,
    handle_cancel_confirm,
    handle_prep_topic,
)
from advisor_agent.orchestrator.preference import has_preference
from advisor_agent.orchestrator.session_store import SessionStore
from advisor_agent.orchestrator.states import State, TransitionNotAllowed, assert_transition_allowed

log = logging.getLogger(__name__)

HANDLERS: dict[State, Handler] = {
    State.DISCLAIMER_ACK: handle_disclaimer,
    State.INTENT_DETECT: handle_intent,
    State.CLARIFY: handle_intent,
    State.TOPIC_CONFIRM: handle_topic,
    State.COLLECT_PREF: handle_collect_pref,
    State.OFFER_SLOTS: handle_offer,
    State.CONFIRM_SLOT: handle_confirm,
    State.ASK_CODE: handle_ask_code,
    State.CANCEL_CONFIRM: handle_cancel_confirm,
    State.PREP_TOPIC: handle_prep_topic,
    State.AVAILABILITY: handle_availability,
    State.EXECUTE: handle_execute,
}
# Where the advice interrupt can return to with "yes, continue" (LLD 5.2).
_RESUMABLE = frozenset(HANDLERS) - {State.DISCLAIMER_ACK, State.INTENT_DETECT, State.CLARIFY,
                                     State.EXECUTE}  # fmt: skip


async def handle_pivot(ctx: SessionContext, nlu: NLUResult, deps: Deps) -> Outcome:
    """AWAIT_PIVOT (LLD 5.2): after an advice refusal, resume the booking or start one."""
    resume = ctx.resume_state if ctx.resume_state in _RESUMABLE else None
    substantive = (
        nlu.slot_choice is not None
        or nlu.topic is not None
        or bool(nlu.topic_candidates)
        or nlu.booking_code is not None
        or has_preference(nlu)
        or (nlu.intent not in (None, Intent.UNKNOWN, Intent.SMALL_TALK)
            and nlu.confidence >= deps.confidence_threshold)
    )  # fmt: skip
    if nlu.yes_no is YesNo.NO and not substantive:
        ctx.resume_state = None
        return Outcome(State.CLOSE, ["GOODBYE"])
    if resume is None:
        if nlu.yes_no is YesNo.YES or substantive:
            ctx.resume_state = None
            enter(ctx, State.INTENT_DETECT)
            if nlu.yes_no is YesNo.YES and not substantive:
                ctx.flow = Flow.BOOK
                return await start_booking(ctx, nlu, deps)
            return await handle_intent(ctx, nlu, deps)
        return Outcome(State.AWAIT_PIVOT, ["PIVOT_REPROMPT"])
    if nlu.yes_no is YesNo.YES and not substantive:
        ctx.resume_state = None
        return Outcome(resume, reprompt(ctx, resume))
    if substantive or nlu.yes_no is not None:
        ctx.resume_state = None
        enter(ctx, resume)
        return await HANDLERS[resume](ctx, nlu, deps)
    return Outcome(State.AWAIT_PIVOT, ["PIVOT_REPROMPT"])


HANDLERS[State.AWAIT_PIVOT] = handle_pivot


@dataclass
class TurnResult:
    """The plan's AgentTurn: plain-string messages plus routing metadata."""

    messages: list[str]
    state: State
    booking_code: str | None = None
    secure_url: str | None = None
    done: bool = False
    template_ids: list[str] = field(default_factory=list)
    offered_count: int = 0
    nlu: NLUResult | None = None


class Orchestrator:
    def __init__(
        self,
        *,
        store: SessionStore,
        nlu: NluEngine,
        deps: Deps,
        audit: AuditSink,
        strict: bool = True,  # dev/test: raise on output/transition violations; prod: SAFE_FALLBACK
    ) -> None:
        self._store = store
        self._nlu = nlu
        self._deps = deps
        self._audit = audit
        self._strict = strict

    async def handle(self, user_text: str | None, session_id: str) -> TurnResult:
        """Alias matching the plan's Orchestrator.handle(user_text, session)."""
        return await self.handle_turn(session_id, user_text)

    async def handle_turn(self, session_id: str, user_text: str | None) -> TurnResult:
        ctx = self._store.load(session_id) or SessionContext(session_id=session_id)
        ctx.turn_no += 1

        if ctx.state is State.START:
            opening = ["GREET", "DISCLAIMER", "DISCLAIMER_ASK_ACK"]
            return self._finish(ctx, State.DISCLAIMER_ACK, opening)

        redacted, hits = pii.redact(user_text or "")
        self._record(ctx, "turn", text=redacted, pii=[h.kind for h in hits])

        if ctx.state is State.CLOSE:
            return self._finish(ctx, State.CLOSE, ["SESSION_ENDED"])
        if hits:
            return self._finish(ctx, ctx.state, ["PII_DEFLECT", *reprompt(ctx)])

        # In AWAIT_PIVOT, read answers as the interrupted question would ("the first one").
        pivoting = ctx.state is State.AWAIT_PIVOT and ctx.resume_state is not None
        parse_state = ctx.resume_state if pivoting and ctx.resume_state else ctx.state
        nlu_ctx = NLUContext(
            state=parse_state.value,
            flow=ctx.flow.value if ctx.flow else None,
            offered_slots=ctx.offered_slots,
            ref=self._deps.clock(),
        )
        nlu = await self._nlu.parse(redacted, parse_state, nlu_ctx)
        # "yes, continue" / "sure" (to a suggested weekday) is not asked by every state.
        if (pivoting or ctx.suggested_pref is not None) and nlu.yes_no is None:
            nlu = nlu.model_copy(update={"yes_no": rules_yes_no(redacted)})

        if nlu.meta == "repeat":
            if ctx.last_messages:
                return self._finish(ctx, ctx.state, [], raw=list(ctx.last_messages), nlu=nlu)
            return self._finish(ctx, ctx.state, reprompt(ctx), nlu=nlu)
        if nlu.meta == "stop":
            return self._finish(ctx, State.CLOSE, ["GOODBYE"], nlu=nlu)
        if nlu.meta == "help":
            hints = ["STUB_HELP"] if self._deps.stub_hints else []
            return self._finish(ctx, ctx.state, [*hints, *reprompt(ctx)], nlu=nlu)

        if self._is_advice(redacted, nlu):
            return self._advice(ctx, nlu)

        work = ctx.model_copy(deep=True)  # handlers mutate a copy; ctx stays intact on failure
        try:
            outcome = await HANDLERS[ctx.state](work, nlu, self._deps)
            if outcome.next_state is State.EXECUTE and work.state is not State.EXECUTE:
                outcome = await self._execute(work, outcome, nlu)
            return self._finish(
                work, outcome.next_state, outcome.templates, outcome.params, nlu=nlu
            )
        except (OutputViolation, TransitionNotAllowed):
            if self._strict:
                raise
            log.exception("policy violation")
        except Exception:
            log.exception("handler failed")
        self._record(ctx, "error", state=ctx.state.value)
        return self._finish(ctx, ctx.state, ["SAFE_FALLBACK", *reprompt(ctx)], nlu=nlu)

    def _is_advice(self, text: str, nlu: NLUResult) -> bool:
        """Global advice interrupt (LLD 5.2): rules first, then the NLU intent."""
        signal = detect_advice(text)
        if signal.is_advice:
            return True
        return (
            nlu.intent is Intent.INVESTMENT_ADVICE
            and nlu.confidence >= self._deps.confidence_threshold
        )

    def _advice(self, ctx: SessionContext, nlu: NLUResult) -> TurnResult:
        links = {"edu_links": edu_links_text()}
        self._record(ctx, "advice_refused", state=ctx.state.value)
        if not ctx.disclaimer_acknowledged:
            return self._finish(
                ctx, ctx.state, ["ADVICE_REFUSAL", "EDU_LINKS", *reprompt(ctx)], links, nlu=nlu
            )
        if ctx.state is State.AWAIT_PIVOT:
            ids = ["ADVICE_REFUSAL_SHORT"]
        else:
            ctx.resume_state = ctx.state if ctx.state in _RESUMABLE else None
            ids = ["ADVICE_REFUSAL", "EDU_LINKS"]
        ids.append("PIVOT_OFFER" if ctx.resume_state else "PIVOT_OFFER_NEW")
        return self._finish(ctx, State.AWAIT_PIVOT, ids, links, nlu=nlu)

    async def _execute(self, work: SessionContext, lead: Outcome, nlu: NLUResult) -> Outcome:
        """EXECUTE is a transient state: entered and left within the same turn (LLD 3.12)."""
        assert_transition_allowed(
            work.state, State.EXECUTE, disclaimer_acknowledged=work.disclaimer_acknowledged
        )
        work.state = State.EXECUTE
        self._record(work, "transition", to=State.EXECUTE.value, templates=lead.templates)
        result = await handle_execute(work, nlu, self._deps)
        return Outcome(
            result.next_state,
            [*lead.templates, *result.templates],
            {**lead.params, **result.params},
        )

    def _finish(
        self,
        ctx: SessionContext,
        next_state: State,
        template_ids: list[str],
        params: dict[str, Any] | None = None,
        *,
        raw: list[str] | None = None,
        nlu: NLUResult | None = None,
    ) -> TurnResult:
        assert_transition_allowed(
            ctx.state, next_state, disclaimer_acknowledged=ctx.disclaimer_acknowledged
        )
        ctx.state = next_state
        msgs = raw if raw is not None else templates.render(template_ids, ctx, params)
        checked = output_check.check(
            msgs,
            next_state.value,
            strict=self._strict,
            template_ids=None if raw is not None else template_ids,
        )
        if checked is None:
            template_ids = ["SAFE_FALLBACK"]
            msgs = templates.render(template_ids, ctx)
            self._record(ctx, "error", reason="output_violation")
        ctx.last_messages = msgs
        self._store.save(ctx)
        self._record(ctx, "transition", to=next_state.value, templates=template_ids)
        return TurnResult(
            messages=msgs,
            state=next_state,
            booking_code=ctx.booking_code,
            secure_url=ctx.secure_url,
            done=next_state is State.CLOSE,
            template_ids=template_ids,
            offered_count=len(ctx.offered_slots),
            nlu=nlu,
        )

    def _record(self, ctx: SessionContext, kind: str, **data: Any) -> None:
        self._audit.record(
            AuditEvent(at=self._deps.clock(), session_id=ctx.session_id, kind=kind, data=data)
        )
