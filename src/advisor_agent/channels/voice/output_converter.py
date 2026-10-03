"""Output converter (LLD 7.3): ChatReply.messages -> one SSML document for Google TTS.

Chat copy is final; this only changes how it sounds, never what it says.
"""

import re
from html import escape

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_SLOT_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (\d{1,2}) "
    rf"({_MONTHS}) \d{{4}}, (\d{{1,2}}):(\d{{2}}) (AM|PM) IST\b"
)
_CODE_RE = re.compile(r"\b([A-Z]{2})-([A-Z0-9]{4})\b")
_URL_RE = re.compile(r"(?::\s*)?\(?(https?://[^\s);]+)\)?")
_ABBREV_RE = re.compile(r"\b(KYC|SIP)(s?)\b")
_ORDINAL = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}

SECURE_LINK_SPOKEN = "I've put the secure link on your screen"
LINKS_SPOKEN = "I've put the links on your screen."


def _ordinal(day: int) -> str:
    return f"{day}{_ORDINAL.get(day, 'th')}"


def _spoken_slot(m: re.Match[str]) -> str:
    weekday, day, month, hour, minute, ampm = m.groups()
    clock = hour if minute == "00" else f"{hour}:{minute}"
    return f"{weekday}, the {_ordinal(int(day))} of {month}, at {clock} {ampm}, India Standard Time"


def _say_code(prefix: str, rest: str) -> str:
    chars = f'<say-as interpret-as="characters">{prefix}{rest}</say-as>'
    return f'<prosody rate="85%">{chars}</prosody>'


def _normalise_text(text: str, secure_url: str | None) -> str:
    text = text.replace("**", "")
    other_links = False

    def url(m: re.Match[str]) -> str:
        nonlocal other_links
        raw = m.group(1).rstrip(".,;")
        if secure_url and raw == secure_url:
            return ". " + SECURE_LINK_SPOKEN + "."
        other_links = True
        return ""

    text = _URL_RE.sub(url, text)
    text = text.replace("here in the chat", "on this call")
    if other_links:
        text = text.replace(";", ",")
    text = _SLOT_RE.sub(_spoken_slot, text)
    text = re.sub(r"\s*\(IST\)", "", text)
    text = re.sub(r"\bIST\b", "India Standard Time", text)
    text = re.sub(r"\(yes\s*/\s*no\)", "Please say yes or no.", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    text = re.sub(r"[,;:]+(?=[.,])", "", text)
    text = re.sub(r"\.{2,}", ".", text).strip()
    if other_links:
        text = text.rstrip(":,; .") + ". " + LINKS_SPOKEN
    return text


def _message_to_ssml(text: str, secure_url: str | None) -> str:
    plain = _normalise_text(text, secure_url)
    ssml = escape(plain, quote=False)
    ssml = re.sub(r"\b1\) ", "option one, ", ssml)
    ssml = re.sub(r"\bor 2\) ", '<break time="250ms"/>or option two, ', ssml)
    ssml = ssml.replace(" — ", ', <break time="150ms"/>')
    ssml = re.sub(r"\b([A-Za-z]+)/([A-Za-z]+)\b", r"\1 and \2", ssml)
    ssml = _ABBREV_RE.sub(r'<say-as interpret-as="characters">\1</say-as>\2', ssml)

    def code(m: re.Match[str]) -> str:
        spoken = _say_code(*m.groups())
        if re.search(r"code is\s*$", ssml[: m.start()]):
            # LLD 7.3: a newly issued code is read twice.
            return f'{spoken}<break time="400ms"/>, that\'s {spoken}<break time="300ms"/>'
        return spoken

    ssml = _CODE_RE.sub(code, ssml)
    if "not investment advice" in plain:
        ssml = f'<prosody rate="95%">{ssml}</prosody>'
    return ssml


def to_ssml(messages: list[str], secure_url: str | None = None) -> str:
    """One <speak> document for a whole turn, with a short pause between messages."""
    parts = [_message_to_ssml(m, secure_url) for m in messages if m.strip()]
    return "<speak>" + '<break time="300ms"/>'.join(parts) + "</speak>"


def to_plain_speech(messages: list[str], secure_url: str | None = None) -> str:
    """The spoken wording without SSML (for captions, logs and tests)."""
    return " ".join(_normalise_text(m, secure_url) for m in messages if m.strip())
