"""Input converter (LLD 7.2): an STT transcript -> the same `user_text` a chat user would type.

Spoken PII is deliberately passed through unchanged so the core PII guard handles it exactly as
it does in chat.
"""

import re

from advisor_agent.domain.codes import parse_spoken_or_typed

LOW_CONFIDENCE = 0.6
SHORT_ANSWER_WORDS = 3

_FILLERS = {"um", "umm", "uh", "uhh", "hmm", "hm", "mm", "er", "ah", "oh"}
_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
_MINUTES = {
    "fifteen": "15", "thirty": "30", "forty five": "45", "forty-five": "45", "o'clock": "00",
}  # fmt: skip
_MERIDIEM_RE = re.compile(r"\b([ap])\.\s?m\.?(?=\W|$)", re.IGNORECASE)
_WORD_TIME_RE = re.compile(
    rf"\b({'|'.join(_HOURS)})(?:\s+({'|'.join(map(re.escape, _MINUTES))}))?"
    r"(?:\s+([ap]m))?\b",
    re.IGNORECASE,
)


def _word_time(m: re.Match[str]) -> str:
    hour_word, minute_word, meridiem = m.groups()
    if not minute_word and not meridiem:
        return m.group(0)  # "the second one", "one of them": not a time
    hour = _HOURS[hour_word.lower()]
    minutes = _MINUTES[minute_word.lower()] if minute_word else None
    if minutes == "00":
        return f"{hour} o'clock"
    out = f"{hour}:{minutes}" if minutes else str(hour)
    return f"{out} {meridiem.lower()}" if meridiem else out


def to_user_text(transcript: str, confidence: float | None = None) -> str | None:
    """Return the text to send to ChatService, "repeat" to make the agent re-ask, or None."""
    text = " ".join(transcript.split()).strip()
    words = re.findall(r"[a-z0-9']+", text.lower())
    if not words or all(w in _FILLERS for w in words):
        return None
    if confidence is not None and confidence < LOW_CONFIDENCE and len(words) <= SHORT_ANSWER_WORDS:
        return "repeat"

    text = _MERIDIEM_RE.sub(lambda m: f"{m.group(1).lower()}m", text)
    text = _WORD_TIME_RE.sub(_word_time, text)

    code = parse_spoken_or_typed(text)
    if code and code not in text.upper():
        text = f"{text.rstrip('.')} {code}"
    return text
