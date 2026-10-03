"""Output checker (LLD 2.7): last line of defence on every assistant message."""

import re
from collections.abc import Iterable

from advisor_agent.guardrails import pii

FULL_SLOT_RE = re.compile(r"\w+day, \d{1,2} \w+ \d{4}, \d{1,2}:\d{2} (?:AM|PM) IST")
# A requested day/window label such as '(around 3:00 PM)' next to an explicit IST mention.
_PREF_WINDOW_RE = re.compile(r"\((?:around|after|before|between|from) [^)]*\)", re.IGNORECASE)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2} ?(?:AM|PM)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_CODE_RE = re.compile(r"\bNL-[A-Z]\d{3}\b")
_ADVICE_PHRASES = re.compile(
    r"you should invest|i recommend|i'd recommend|good fund|best fund|will give returns",
    re.IGNORECASE,
)
# close: booked read-back; cancel_confirm: the slot being cancelled.
SLOT_STATES = frozenset({"offer_slots", "confirm_slot", "cancel_confirm", "close"})
CODE_TEMPLATES = frozenset({"READ_CODE", "READ_WAITLIST_CODE"})


class OutputViolation(Exception):
    pass


def violations(
    messages: Iterable[str], state: str, template_ids: Iterable[str] | None = None
) -> list[str]:
    messages = list(messages)
    found: list[str] = []
    for msg in messages:
        # Rule 1: no PII (the booking code and secure URL are allowed).
        if pii.contains_pii(_URL_RE.sub("", msg)):
            found.append(f"pii in output: {msg!r}")
        # Rule 2: no advice phrasing.
        if _ADVICE_PHRASES.search(msg):
            found.append(f"advice phrasing in output: {msg!r}")
        # Rule 3: in slot states every time must be part of a full
        # 'Weekday, D Month YYYY, H:MM PM IST' (or a requested window, with IST stated).
        if state in SLOT_STATES:
            full_spans = [m.span() for m in FULL_SLOT_RE.finditer(msg)]
            if "IST" in msg:
                full_spans += [m.span() for m in _PREF_WINDOW_RE.finditer(msg)]
            for t in _TIME_RE.finditer(msg):
                if not any(s <= t.start() and t.end() <= e for s, e in full_spans):
                    found.append(f"slot without full date/IST: {msg!r}")
                    break
    # Rule 4: on CLOSE, a booking code is never *handed out* without the secure link. With
    # template ids known, only READ_CODE / READ_WAITLIST_CODE count (a cancellation may name
    # the cancelled code without a link).
    reads_code = template_ids is None or bool(CODE_TEMPLATES & set(template_ids))
    if state == "close" and reads_code:
        joined = "\n".join(messages)
        if _CODE_RE.search(joined) and not _URL_RE.search(joined):
            found.append("booking code on close without the secure link")
    return found


def check(
    messages: list[str],
    state: str,
    *,
    strict: bool,
    template_ids: Iterable[str] | None = None,
) -> list[str] | None:
    """Return the messages if they pass. Otherwise raise OutputViolation when `strict`
    (dev/test), or return None so the caller substitutes SAFE_FALLBACK (prod)."""
    problems = violations(messages, state, template_ids)
    if not problems:
        return messages
    if strict:
        raise OutputViolation("; ".join(problems))
    return None
