"""Domain vocabulary (LLD 1.1). Pure types, no I/O."""

import datetime as dt
from datetime import datetime, time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class Topic(StrEnum):
    KYC_ONBOARDING = "KYC/Onboarding"
    SIP_MANDATES = "SIP/Mandates"
    STATEMENTS_TAX = "Statements/Tax Docs"
    WITHDRAWALS = "Withdrawals & Timelines"
    ACCOUNT_CHANGES = "Account Changes/Nominee"


class Intent(StrEnum):
    BOOK_NEW = "book_new"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    WHAT_TO_PREPARE = "what_to_prepare"
    CHECK_AVAILABILITY = "check_availability"
    INVESTMENT_ADVICE = "investment_advice"
    SMALL_TALK = "small_talk"
    UNKNOWN = "unknown"


class TimeWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: time  # IST, inclusive
    end: time  # IST, exclusive
    label: str  # "morning" | "afternoon" | "evening" | "around 3 PM"


class Preference(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date | None = None  # None => any day in the horizon
    window: TimeWindow | None = None


class Slot(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot_id: str
    start_utc: datetime
    end_utc: datetime

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("naive datetimes are not allowed")
        return v


class BookingKind(StrEnum):
    BOOKING = "booking"
    WAITLIST = "waitlist"


class BookingStatus(StrEnum):
    TENTATIVE = "tentative"
    WAITLIST = "waitlist"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    # Phase 4 compensation: the calendar hold could not be placed
    NEEDS_ATTENTION = "needs_attention"


class Booking(BaseModel):
    code: str
    kind: BookingKind
    topic: Topic
    slot: Slot | None
    preference: Preference | None
    status: BookingStatus
    calendar_event_id: str | None = None
    version: int = 1  # bumped by every reschedule (hold event ids and job keys use it)
    pref_label: str | None = None  # waitlist: the requested day/window, e.g. 'Tuesday, ...'
