"""NLU output schema (LLD 3.2). Introduced in Phase 2 for the stub; Phase 3 engines reuse it."""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from advisor_agent.domain.models import Intent, Slot, Topic
from advisor_agent.domain.timeutil import fmt_date, fmt_slot, fmt_time, to_ist

__all__ = ["Intent", "NLUContext", "NLUResult", "YesNo"]


class YesNo(StrEnum):
    YES = "yes"
    NO = "no"
    UNCLEAR = "unclear"


class NLUResult(BaseModel):
    intent: Intent | None = None
    confidence: float = 0.0  # 0..1
    topic: Topic | None = None
    # Phase 3: several topics mentioned with equal weight -> topic stays None, candidates listed
    topic_candidates: list[Topic] = []
    date_text: str | None = None  # canonical day phrase, e.g. "tuesday", "next tuesday", "any"
    date_iso: date | None = None
    # "morning" | "afternoon" | "evening" | "around 15:00" | "after 16:00" | "before 12:00" |
    # "between 14:00 and 16:00" (canonical forms produced by nlu.date_resolver.extract_time)
    time_text: str | None = None
    slot_choice: Literal[1, 2] | None = None
    yes_no: YesNo | None = None
    booking_code: str | None = None
    meta: Literal["repeat", "help", "stop"] | None = None
    source: Literal["stub", "rules", "llm", "merged"] = "rules"


class NLUContext(BaseModel):
    """What the NLU may know about the conversation. Built by the orchestrator."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: str
    flow: str | None = None
    offered_slots: list[Slot] = []
    ref: datetime  # "now" (aware) used to resolve relative dates

    def prompt_vars(self) -> dict[str, str]:
        """Non-PII values interpolated into the LLM system prompt (LLD 3.3)."""
        now = to_ist(self.ref)
        offered = "; ".join(f"{i}) {fmt_slot(s)}" for i, s in enumerate(self.offered_slots, 1))
        return {
            "today": f"{fmt_date(now.date())} ({now.date().isoformat()})",
            "now_time": f"{fmt_time(now.time().replace(second=0, microsecond=0))} IST",
            "state": str(self.state),
            "flow": self.flow or "none",
            "offered_slots": offered or "none",
        }
