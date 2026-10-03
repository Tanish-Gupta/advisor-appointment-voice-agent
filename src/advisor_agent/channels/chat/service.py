"""Chat Service (LLD 2.11): the only entry point into the core. CLI, HTTP API, web UI and
(Phase 7) the voice bridge are thin clients of start()/send()."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from advisor_agent.domain.models import Topic
from advisor_agent.domain.timeutil import fmt_preference, fmt_slot
from advisor_agent.orchestrator.fsm import Orchestrator, TurnResult
from advisor_agent.orchestrator.session_store import SessionStore
from advisor_agent.orchestrator.states import State

MAX_TEXT_LEN = 500

_QUICK_REPLIES: dict[State, list[str]] = {
    State.DISCLAIMER_ACK: ["yes", "no"],
    State.INTENT_DETECT: ["book", "reschedule", "cancel", "what to prepare", "check availability"],
    State.CLARIFY: ["book", "reschedule", "cancel", "what to prepare", "check availability"],
    State.TOPIC_CONFIRM: [t.value for t in Topic],
    State.COLLECT_PREF: ["tomorrow morning", "monday afternoon", "any"],
    State.CONFIRM_SLOT: ["yes", "no"],
}
# What the web UI shows (and sends) for each quick reply: natural phrasings the NLU already
# understands, so chips read like conversation rather than a menu. Free text works just as well.
_SUGGESTION_LABELS: dict[str, str] = {
    "yes": "Yes, that's fine",
    "no": "No thanks",
    "book": "Book a call",
    "reschedule": "Move my booking",
    "cancel": "Cancel my booking",
    "what to prepare": "What should I bring?",
    "check availability": "When are advisors free?",
    "KYC/Onboarding": "KYC / onboarding",
    "SIP/Mandates": "SIPs & mandates",
    "Statements/Tax Docs": "Statements & tax docs",
    "Withdrawals & Timelines": "Withdrawals",
    "Account Changes/Nominee": "Nominee / account changes",
    "tomorrow morning": "Tomorrow morning",
    "monday afternoon": "Monday afternoon",
    "any": "Earliest available",
    "Option 1": "The first one",
    "Option 2": "The second one",
    "neither": "Neither works",
}
_STATE_LABELS: dict[State, dict[str, str]] = {
    State.CONFIRM_SLOT: {"yes": "Yes, book it", "no": "No, change it"},
    State.OFFER_SLOTS: {"yes": "Yes please", "neither": "Another time"},
}


def _label(state: State, value: str) -> str:
    return _STATE_LABELS.get(state, {}).get(value) or _SUGGESTION_LABELS.get(value, value)


class ChatReply(BaseModel):
    session_id: str
    messages: list[str]
    state: State
    booking_code: str | None = None
    secure_url: str | None = None
    quick_replies: list[str] = []
    suggestions: list[str] = []  # display labels for quick_replies (same order)
    done: bool = False
    # Structured view of the booking so far for the web UI (booking card, slot cards, voice
    # captions). Holds no user text or PII: only the flow, topic and IST slot strings.
    details: dict[str, Any] = {}


class SessionNotFound(Exception):
    pass


class InvalidMessage(ValueError):
    pass


class ChatService:
    def __init__(
        self,
        orchestrator: Orchestrator,
        store: SessionStore,
        *,
        golden_dir: Path | None = None,
        scenario: str | None = None,
        runtime: Any = None,
    ) -> None:
        self._orch = orchestrator
        self._store = store
        self.runtime = runtime  # bootstrap.Runtime when MCP tools are enabled (Phase 3B/4)
        self._locks: dict[str, asyncio.Lock] = {}
        self._golden_path = golden_dir / f"{scenario}.jsonl" if golden_dir and scenario else None
        self.last_turn: TurnResult | None = None  # for CLI --debug

    async def start(self) -> ChatReply:
        session_id = str(uuid.uuid4())
        reply = await self._turn(session_id, None)
        return reply

    async def send(self, session_id: str, text: str) -> ChatReply:
        text = (text or "").strip()
        if not 1 <= len(text) <= MAX_TEXT_LEN:
            raise InvalidMessage(f"text must be 1..{MAX_TEXT_LEN} characters")
        if self._store.load(session_id) is None:
            raise SessionNotFound(session_id)
        return await self._turn(session_id, text)

    def snapshot(self, session_id: str) -> dict[str, object]:
        """Current SessionContext for debugging (holds no user text, so nothing to redact)."""
        ctx = self._store.load(session_id)
        if ctx is None:
            raise SessionNotFound(session_id)
        return ctx.model_dump(mode="json")

    async def _turn(self, session_id: str, text: str | None) -> ChatReply:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            result = await self._orch.handle_turn(session_id, text)
        if result.done:
            self._locks.pop(session_id, None)
        self.last_turn = result
        quick = self._quick_replies(result)
        reply = ChatReply(
            session_id=session_id,
            messages=result.messages,
            state=result.state,
            booking_code=result.booking_code,
            secure_url=result.secure_url,
            quick_replies=quick,
            suggestions=[_label(result.state, q) for q in quick],
            done=result.done,
            details=self._details(session_id, result),
        )
        self._record(text, reply)
        return reply

    @staticmethod
    def _quick_replies(result: TurnResult) -> list[str]:
        if result.state is State.OFFER_SLOTS:
            two = result.offered_count >= 2
            return ["Option 1", "Option 2", "neither"] if two else ["yes", "neither"]
        return list(_QUICK_REPLIES.get(result.state, []))

    def _details(self, session_id: str, result: TurnResult) -> dict[str, Any]:
        ctx = self._store.load(session_id)
        if ctx is None:
            return {}
        offered = ctx.offered_slots if result.state is State.OFFER_SLOTS else []
        return {
            "flow": ctx.flow.value if ctx.flow else None,
            "topic": ctx.topic.value if ctx.topic else None,
            "preference": fmt_preference(ctx.preference) if ctx.preference else None,
            "offered": [fmt_slot(s) for s in offered],
            "slot": fmt_slot(ctx.chosen_slot) if ctx.chosen_slot else None,
            "current_slot": fmt_slot(ctx.current_slot) if ctx.current_slot else None,
            "waitlist": ctx.waitlist,
            "code": ctx.booking_code,
        }

    def _record(self, text: str | None, reply: ChatReply) -> None:
        if self._golden_path is None:
            return
        self._golden_path.parent.mkdir(parents=True, exist_ok=True)
        dump = reply.model_dump(mode="json", exclude={"session_id", "suggestions", "details"})
        line = {"user": text, "reply": dump}
        with self._golden_path.open("a") as f:
            f.write(json.dumps(line) + "\n")
