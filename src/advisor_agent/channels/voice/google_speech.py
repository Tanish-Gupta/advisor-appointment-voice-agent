"""Google Cloud Speech-to-Text + Text-to-Speech over REST (LLD 7.1).

Auth reuses the project's service account (AGENT_GOOGLE_SERVICE_ACCOUNT_FILE) with the
cloud-platform scope. Calls are blocking; the HTTP routes run them in a worker thread.
Audio is never stored or logged.
"""

import base64
from dataclasses import dataclass
from typing import Any, Protocol

from advisor_agent.config import Settings

STT_URL = "https://speech.googleapis.com/v1/speech:recognize"
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
TIMEOUT_S = 15.0

# Words the recogniser should prefer in this domain.
PHRASE_HINTS = [
    "KYC", "onboarding", "SIP", "SIPs", "mandate", "mandates", "statements", "tax documents",
    "withdrawal", "withdrawals", "nominee", "account changes", "advisor", "reschedule",
    "cancel", "booking code", "the first one", "the second one", "tomorrow morning",
    "afternoon", "evening", "IST",
]  # fmt: skip


class SpeechError(RuntimeError):
    """A speech call failed; `detail` is safe to show to the user."""

    def __init__(self, detail: str, *, status: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.google_message = ""


@dataclass(frozen=True)
class SttResult:
    transcript: str
    confidence: float | None


class SpeechBackend(Protocol):
    name: str
    voice: str

    def transcribe(self, wav: bytes, sample_rate: int) -> SttResult: ...

    def synthesize(self, ssml: str) -> bytes: ...


def _explain(status: int, body: Any, api: str) -> SpeechError:
    message = ""
    if isinstance(body, dict):
        message = str(body.get("error", {}).get("message", ""))
    if status == 403 and ("has not been used" in message or "disabled" in message):
        return SpeechError(
            f"The {api} API is not enabled for this Google Cloud project.", status=503
        )
    if status in (401, 403):
        return SpeechError(f"The service account is not allowed to use the {api} API.", status=503)
    if status == 429:
        return SpeechError(f"{api} quota exceeded; try again shortly.", status=503)
    error = SpeechError(f"{api} request failed ({status}).", status=502)
    error.google_message = message
    return error


class GoogleSpeech:
    name = "google"

    def __init__(
        self,
        session: Any,
        *,
        language: str = "en-IN",
        voice: str = "en-IN-Neural2-A",
        speaking_rate: float = 1.0,
        stt_model: str | None = "latest_short",
    ) -> None:
        self._session = session  # a google.auth AuthorizedSession (or a test double)
        self.language = language
        self.voice = voice
        self.speaking_rate = speaking_rate
        self.stt_model = stt_model

    def _post(self, url: str, payload: dict[str, Any], api: str) -> dict[str, Any]:
        try:
            res = self._session.post(url, json=payload, timeout=TIMEOUT_S)
        except Exception as e:  # network, DNS, token refresh
            raise SpeechError(f"Could not reach Google {api}.", status=503) from e
        try:
            body = res.json()
        except ValueError:
            body = None
        if res.status_code != 200:
            raise _explain(res.status_code, body, api)
        return body or {}

    def transcribe(self, wav: bytes, sample_rate: int) -> SttResult:
        config: dict[str, Any] = {
            "encoding": "LINEAR16",
            "sampleRateHertz": sample_rate,
            "languageCode": self.language,
            "enableAutomaticPunctuation": True,
            "speechContexts": [{"phrases": PHRASE_HINTS}],
        }
        audio = {"content": base64.b64encode(wav).decode()}
        model = {"model": self.stt_model} if self.stt_model else {}
        try:
            body = self._post(STT_URL, {"config": config | model, "audio": audio}, "Speech-to-Text")
        except SpeechError as e:
            if not (model and "model" in e.google_message.lower()):
                raise
            # Some language/model pairs are not available; fall back to the default model.
            self.stt_model = None
            body = self._post(STT_URL, {"config": config, "audio": audio}, "Speech-to-Text")

        texts: list[str] = []
        confidences: list[float] = []
        for result in body.get("results", []):
            alts = result.get("alternatives") or []
            if alts and alts[0].get("transcript"):
                texts.append(alts[0]["transcript"].strip())
                if "confidence" in alts[0]:
                    confidences.append(float(alts[0]["confidence"]))
        return SttResult(" ".join(texts).strip(), min(confidences) if confidences else None)

    def synthesize(self, ssml: str) -> bytes:
        payload = {
            "input": {"ssml": ssml},
            "voice": {"languageCode": self.language, "name": self.voice},
            "audioConfig": {"audioEncoding": "MP3", "speakingRate": self.speaking_rate},
        }
        body = self._post(TTS_URL, payload, "Text-to-Speech")
        audio = body.get("audioContent")
        if not audio:
            raise SpeechError("Text-to-Speech returned no audio.")
        return base64.b64decode(audio)


def load_google_speech(settings: Settings) -> GoogleSpeech:
    """Build the client from the service account; raises SpeechError if it is not configured."""
    if not settings.google_service_account_file:
        raise SpeechError("Set AGENT_GOOGLE_SERVICE_ACCOUNT_FILE to use Google speech.", status=503)
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            settings.google_service_account_file, scopes=SCOPES
        )
    except (OSError, ValueError) as e:
        raise SpeechError("The Google service account file could not be read.", status=503) from e
    return GoogleSpeech(
        AuthorizedSession(creds),
        language=settings.voice_language,
        voice=settings.voice_tts_voice,
        speaking_rate=settings.voice_speaking_rate,
        stt_model=settings.voice_stt_model or None,
    )
