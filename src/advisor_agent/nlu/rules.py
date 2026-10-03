"""RulesNLU (LLD 3.5): deterministic, offline understanding of natural phrases.

First stage of the hybrid pipeline. Anything it recognises is returned with confidence 0.95;
anything it does not is left empty so the LLM (Gemini) can fill the gap. Also usable on its own
(AGENT_NLU_ENGINE=rules) for offline demos and CI.
"""

import re
from datetime import datetime

from advisor_agent.domain import codes
from advisor_agent.domain.models import Intent, Slot
from advisor_agent.domain.timeutil import to_ist
from advisor_agent.guardrails.advice import ADVICE_PATTERNS
from advisor_agent.nlu.date_resolver import (
    WEEKDAYS,
    RelativeDateResolver,
    clock_times,
    extract_date,
    extract_time,
)
from advisor_agent.nlu.schema import NLUContext, NLUResult, YesNo
from advisor_agent.nlu.topic_mapper import match_topic, pick_from_list

RULE_CONFIDENCE = 0.95


# --------------------------------------------------------------------------- normalisation


def normalise(text: str) -> str:
    """Lower-case; keep letters, digits and `: / ' & -`; collapse whitespace."""
    t = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", t)  # a.m. / p.m.
    t = re.sub(r"(\d)\.(\d{2})\b", r"\1:\2", t)  # 3.30 -> 3:30
    t = re.sub(r"[^a-z0-9\s:/'&-]", " ", t)
    t = re.sub(r"(?<![a-z0-9])-|-(?![a-z0-9])", " ", t)  # keep hyphens inside words only
    return re.sub(r"\s+", " ", t).strip()


_GREETING_PREFIX = re.compile(
    r"^(?:(?:hi|hello|hey|hii|namaste)\b\s*(?:there\b\s*)?)?"
    r"(?:good (?:morning|afternoon|evening|day)\b\s*)?"
)

# --------------------------------------------------------------------------- vocabulary

_META: list[tuple[str, re.Pattern[str]]] = [
    ("repeat", re.compile(
        r"(?:please |can you |could you |sorry |)(?:repeat(?: that| it| again)?|say (?:that |it )?"
        r"again|pardon(?: me)?|come again|what did you say|i didn'?t (?:catch|get|hear) (?:that|it)"
        r"|again)(?: please)?"
    )),
    ("help", re.compile(
        r"(?:help|help me|commands|menu|options|what can you do|what are (?:my|the) options"
        r"|how does this work|what do you do)(?: please)?"
    )),
    ("stop", re.compile(
        r"(?:stop|bye|bye bye|goodbye|good bye|exit|quit|end|end chat|end this|that'?s all"
        r"|that is all|nothing else|no thanks bye|thanks bye|thank you bye|leave it|forget it)"
    )),
]  # fmt: skip
_SMALL_TALK = re.compile(
    r"(?:hi|hello|hey|hii|namaste|yo|good (?:morning|afternoon|evening|day)|how are you"
    r"|thanks|thank you|thank you so much|thanks a lot|cool|nice|great thanks|ok thanks"
    r"|okay thanks|who are you|are you a bot|are you human)(?: there)?"
)

_YES_ONLY = re.compile(r"y|k|ok|okay|yes|yea|yeah|ya|yup|yep|sure")
_YES_START = re.compile(
    r"^(?:yes|yeah|yea|yep|yup|ya|sure|ok|okay|okey|alright|all right|fine|correct|right|exactly"
    r"|absolutely|definitely|of course|certainly|please do|do it|confirm|confirmed|go ahead"
    r"|sounds good|that works|works for me|perfect|great|i understand|i agree|understood|agreed"
    r"|no problem|no worries|why not|i do(?! not)|i accept|accepted|noted|got it|proceed|continue"
    r"|let'?s do it|let'?s go|go for it|book it|hold it|that'?s fine|that is fine)\b"
)
_YES_ANY = re.compile(
    r"\b(?:i understand|i agree|go ahead|book it|hold it|confirm it|lock it|yes|understood"
    r"|that works|sounds good|please proceed|that'?s right|that'?s correct)\b"
)
_NO_ONLY = re.compile(r"n|no|nope|nah|na")
_NO_START = re.compile(
    r"^(?:no(?! (?:problem|worries|preference|issue))|nope|nah|neither|none|not really"
    r"|i don'?t(?! mind)|don'?t(?! mind)|i do not|negative|not that|not now|i disagree"
    r"|wrong|incorrect)\b"
)
_NO_ANY = re.compile(
    r"\b(?:neither|none of (?:them|these|those|the two|both)|other options?|other slots?"
    r"|something else|anything else|different (?:time|day|slot|date)|another (?:time|day|slot"
    r"|option|date)|not (?:those|these|that one|this one)|(?:doesn'?t|don'?t|won'?t|does not"
    r"|do not|will not) work|not okay|not ok|not convenient|can'?t make (?:it|that|those))\b"
)

_ADVICE = ADVICE_PATTERNS  # shared with the global advice interrupt (guardrails/advice.py)
_CANCEL = re.compile(
    r"\b(?:cancel\w*|call off|calling off|drop my (?:booking|appointment|slot)"
    r"|don'?t need the (?:appointment|slot|booking|call) anymore)\b"
)
_CANCEL_TOPIC_OBJECT = re.compile(
    r"\bcancel\w* (?:my |the |a |an |our )?(?:sip|sips|mandate|mandates|e-mandate|nach|autopay"
    r"|auto debit|auto-debit|order|redemption|withdrawal|nomination|nominee|kyc)\b"
)
_BOOKING_NOUN = re.compile(
    r"\b(?:appointment|booking|slot|meeting|call|consultation|session|hold|reservation)\b"
)
_RESCHEDULE = re.compile(
    r"\b(?:re-?schedul\w*|re-?book\w*|postpon\w*|prepon\w*|re-?arrang\w*)\b"
    r"|\b(?:move|shift|change|push|switch|modify|update|bring forward)\b (?:my |the |our |this )?"
    r"(?:existing |current |booked |earlier )?(?:slot|time|timing|booking|appointment|meeting"
    r"|call|date|consultation|session)\b"
)
_PREPARE = re.compile(
    r"\bwhat (?:do|should|shall|must|will) i (?:need to |have to )?(?:prepare|bring|keep ready"
    r"|have ready|carry|get ready|keep handy|need)\b"
    r"|\b(?:prepare|preparing|preparation|prep)\b"
    r"|\b(?:documents?|docs|papers|paperwork) (?:do|should|would|will) i (?:need|bring|keep)\b"
    r"|\bwhat (?:documents?|docs|papers|paperwork)\b"
    r"|\b(?:checklist|keep ready|have handy|be ready with)\b"
)
_AVAILABILITY = re.compile(
    r"\b(?:availab\w*|free slots?|open slots?|slots? (?:are |is )?(?:free|open|left)"
    r"|when (?:are|is|can) (?:the |an |your )?advisors?|what (?:slots|times|days|timings)"
    r"|which (?:slots|times|days|timings)|any (?:slots|openings|free time)|openings"
    r"|check (?:the )?(?:calendar|schedule|times|timings|slots))\b"
)
_EXPLICIT_BOOK = re.compile(r"\b(?:book\w*|reserve\w*|schedul\w*|hold a|block a)\b")
_BOOK = re.compile(
    r"\b(?:book\w*|schedul\w*|appointment|slot|consult\w*|talk to|speak (?:to|with)|meet"
    r"|meeting|call ?back|set up|arrange|fix (?:a|an|up)|reserve|an? advisor|the advisor"
    r"|advisor call|session|sit down with|get in touch|discuss|chat with|connect (?:me|with))\b"
)

_SLOT_ORDINAL = re.compile(
    r"\b(?:the )?(first|second|1st|2nd|former|latter|earlier one|later one|earlier slot"
    r"|later slot|top one|bottom one|last one)(?! thing| available| week| half)\b"
)
_SLOT_NUMBER = re.compile(r"\b(?:option|slot|number|no|choice|#)\s*(1|2|one|two)\b")
_SLOT_BARE = re.compile(r"(?:the )?(1|2|one|two)(?: please| it is| works)?")
_SLOT_WORD = {"first": 1, "1st": 1, "former": 1, "earlier one": 1, "earlier slot": 1,
              "top one": 1, "second": 2, "2nd": 2, "latter": 2, "later one": 2,
              "later slot": 2, "bottom one": 2, "last one": 2, "1": 1, "one": 1, "2": 2,
              "two": 2}  # fmt: skip
_WD_RE = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_WEEKDAY_CHOICE = re.compile(rf"\b(?:the )?({_WD_RE})(?:'s)? (?:one|slot|option|time|it is)\b")
_WEEKDAY_WORD = re.compile(rf"\b({_WD_RE})\b")
_TOPIC_STATES = {"topic_confirm", "prep_topic"}
_YES_NO_STATES = {"disclaimer_ack", "confirm_slot", "cancel_confirm", "offer_slots",
                  "intent_detect", "clarify", "await_pivot"}  # fmt: skip


def _yes_no(t: str) -> YesNo | None:
    yes = bool(_YES_ONLY.fullmatch(t) or _YES_START.search(t) or _YES_ANY.search(t))
    no = bool(_NO_ONLY.fullmatch(t) or _NO_START.search(t) or _NO_ANY.search(t))
    if yes and no:
        return YesNo.UNCLEAR
    if yes:
        return YesNo.YES
    if no:
        return YesNo.NO
    return None


def yes_no(text: str) -> YesNo | None:
    """Public yes/no reader for a raw utterance (used by AWAIT_PIVOT, LLD 5.2)."""
    return _yes_no(normalise(text))


def detect_intent(t: str) -> Intent | None:
    """Priority: advice > cancel > reschedule > what_to_prepare > availability|book."""
    if any(p.search(t) for p in _ADVICE):
        return Intent.INVESTMENT_ADVICE
    if _CANCEL.search(t) and not (_CANCEL_TOPIC_OBJECT.search(t) and not _BOOKING_NOUN.search(t)):
        return Intent.CANCEL
    if _RESCHEDULE.search(t):
        return Intent.RESCHEDULE
    if _PREPARE.search(t):
        return Intent.WHAT_TO_PREPARE
    if _AVAILABILITY.search(t) and not _EXPLICIT_BOOK.search(t):
        return Intent.CHECK_AVAILABILITY
    if _BOOK.search(t):
        return Intent.BOOK_NEW
    return None


def slot_choice(t: str, offered: list[Slot], time_text: str | None) -> int | None:
    """'the second one', 'option 2', '2', 'the 3 pm one', 'the tuesday one' -> 1 | 2."""
    n = len(offered)
    choice: int | None = None
    if m := _SLOT_NUMBER.search(t):
        choice = _SLOT_WORD[m.group(1)]
    elif m := _SLOT_BARE.fullmatch(t):
        choice = _SLOT_WORD[m.group(1)]
    elif m := _SLOT_ORDINAL.search(t):
        choice = _SLOT_WORD[m.group(1)]
        if m.group(1) in ("last one", "bottom one", "latter", "later one", "later slot"):
            choice = n or choice
    elif offered:
        choice = _match_offered(t, offered, time_text)
    if choice is not None and 1 <= choice <= max(n, 2):
        return choice
    return None


def _match_offered(t: str, offered: list[Slot], time_text: str | None) -> int | None:
    starts = [to_ist(s.start_utc) for s in offered]
    candidates = list(range(len(offered)))
    filtered = False
    if wd := _WEEKDAY_WORD.search(t):
        day = WEEKDAYS[wd.group(1)]
        candidates = [i for i in candidates if starts[i].weekday() == day]
        filtered = True
    times = clock_times(t)
    if times:
        candidates = [i for i in candidates if starts[i].time() in times]
        filtered = True
    elif filtered and not _WEEKDAY_CHOICE.search(t):
        return None  # "tuesday afternoon" is a new preference, not a pick
    if filtered and len(candidates) == 1:
        return candidates[0] + 1
    return None


class RulesNLU:
    """Implements NluEngine."""

    def __init__(self, resolver: RelativeDateResolver | None = None) -> None:
        self.resolver = resolver or RelativeDateResolver()

    async def parse(self, transcript: str, state: str, ctx: NLUContext) -> NLUResult:
        return self.parse_sync(transcript, str(state), ctx.offered_slots, ctx.ref)

    def parse_sync(
        self, transcript: str, state: str, offered: list[Slot], ref: datetime
    ) -> NLUResult:
        t = normalise(transcript)
        hit = NLUResult(source="rules", confidence=RULE_CONFIDENCE)

        for meta, pattern in _META:
            if pattern.fullmatch(t):
                hit.meta = meta  # type: ignore[assignment]
                return hit
        if _SMALL_TALK.fullmatch(t):
            hit.intent = Intent.SMALL_TALK
            return hit

        body = _GREETING_PREFIX.sub("", t, count=1).strip() or t
        result = NLUResult(source="rules")

        if state in _YES_NO_STATES:
            result.yes_no = _yes_no(body)

        result.intent = detect_intent(body)

        if state in _TOPIC_STATES and (picked := pick_from_list(body)) is not None:
            result.topic = picked
        else:
            topic, candidates = match_topic(body)
            result.topic = topic
            result.topic_candidates = candidates if topic is None and len(candidates) > 1 else []
        if result.intent is Intent.INVESTMENT_ADVICE:
            result.topic, result.topic_candidates = None, []

        date_text = extract_date(body)
        if state == "offer_slots" and date_text in ("the 1st", "the 2nd"):
            date_text = None  # "the 2nd" = the second option, not the 2nd of the month
        if date_text is not None:
            result.date_text = date_text
            result.date_iso = self.resolver.resolve_date(date_text, ref)
        result.time_text = extract_time(body)

        if state == "offer_slots":
            result.slot_choice = slot_choice(body, offered, result.time_text)  # type: ignore[assignment]

        if (
            "nl" in body.split()
            or re.search(r"\bnl-?[a-z]", body)
            or result.intent in (Intent.RESCHEDULE, Intent.CANCEL)
            or state == "ask_code"
        ):
            result.booking_code = codes.parse_spoken_or_typed(transcript)

        signals = (
            result.intent, result.topic, result.topic_candidates, result.date_text,
            result.time_text, result.slot_choice, result.yes_no, result.booking_code,
        )  # fmt: skip
        if any(s for s in signals):
            result.confidence = RULE_CONFIDENCE
        else:
            result.intent = Intent.UNKNOWN
        return result
