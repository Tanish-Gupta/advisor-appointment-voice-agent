"""Voice HTTP routes (LLD 7.4, turn-based web transport).

The browser records one utterance (16-bit mono WAV) and posts it to /transcribe. The input
converter turns the transcript into ordinary `user_text`, which the client sends to the
unchanged chat API. Each ChatReply is then posted to /speak, which returns MP3 audio built
by the output converter (SSML) and Google TTS.
"""

import asyncio
import io
import logging
import threading
import time
import wave
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from advisor_agent.channels.voice.google_speech import (
    SpeechBackend,
    SpeechError,
    load_google_speech,
)
from advisor_agent.channels.voice.input_converter import to_user_text
from advisor_agent.channels.voice.output_converter import to_ssml
from advisor_agent.config import Settings

log = logging.getLogger(__name__)

MAX_SPEAK_MESSAGES = 10
MAX_SPEAK_CHARS = 2500
TTS_CACHE_SIZE = 64
ALLOWED_RATES = {8000, 16000, 22050, 24000, 44100, 48000}


class SpeakIn(BaseModel):
    messages: list[str] = Field(min_length=1, max_length=MAX_SPEAK_MESSAGES)
    secure_url: str | None = Field(default=None, max_length=600)


class _Limiter:
    def __init__(self, per_min: int) -> None:
        self._per_min = per_min
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self._per_min:
            return False
        q.append(now)
        return True


class _Backend:
    """Builds the speech backend on first use, so the app starts without touching Google."""

    def __init__(self, settings: Settings, factory: Callable[[], SpeechBackend] | None) -> None:
        self._settings = settings
        self._factory = factory
        self._backend: SpeechBackend | None = None
        self._lock = threading.Lock()

    def get(self) -> SpeechBackend:
        if self._settings.voice_provider != "google" and self._factory is None:
            raise SpeechError(
                "Cloud speech is switched off (AGENT_VOICE_PROVIDER=browser).", status=503
            )
        with self._lock:
            if self._backend is None:
                factory = self._factory or (lambda: load_google_speech(self._settings))
                self._backend = factory()
            return self._backend

    def status(self) -> tuple[str, str | None]:
        try:
            backend = self.get()
        except SpeechError as e:
            return "browser", e.detail
        return backend.name, None


def _read_wav(data: bytes, max_seconds: int) -> tuple[bytes, int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate, frames = (
                w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            )  # fmt: skip
    except (wave.Error, EOFError):
        raise HTTPException(415, "send audio/wav (16-bit PCM, mono)") from None
    if channels != 1 or width != 2 or rate not in ALLOWED_RATES:
        raise HTTPException(415, "send audio/wav (16-bit PCM, mono)")
    if frames / rate > max_seconds:
        raise HTTPException(413, f"utterance longer than {max_seconds} s")
    return data, rate


def max_audio_bytes(settings: Settings) -> int:
    return settings.voice_max_utterance_s * 48000 * 2 + 1024


def build_voice_router(
    settings: Settings, backend_factory: Callable[[], SpeechBackend] | None = None
) -> APIRouter:
    router = APIRouter(prefix="/v1/voice", tags=["voice"])
    backend = _Backend(settings, backend_factory)
    limiter = _Limiter(settings.voice_requests_per_min_per_ip)
    cache: OrderedDict[str, bytes] = OrderedDict()
    cache_lock = threading.Lock()

    def check_rate(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            raise HTTPException(429, "too many voice requests, please slow down")

    def speech() -> SpeechBackend:
        try:
            return backend.get()
        except SpeechError as e:
            raise HTTPException(e.status, e.detail) from None

    @router.get("/config")
    async def config() -> dict[str, object]:
        provider, reason = await asyncio.to_thread(backend.status)
        return {
            "provider": provider,
            "language": settings.voice_language,
            "voice": settings.voice_tts_voice if provider != "browser" else None,
            "max_utterance_s": settings.voice_max_utterance_s,
            "reason": reason,
        }

    @router.post("/transcribe")
    async def transcribe(request: Request) -> dict[str, object]:
        check_rate(request)
        data = await request.body()
        if len(data) > max_audio_bytes(settings):
            raise HTTPException(413, "audio too large")
        wav, rate = _read_wav(data, settings.voice_max_utterance_s)
        stt = speech()
        try:
            result = await asyncio.to_thread(stt.transcribe, wav, rate)
        except SpeechError as e:
            log.warning("voice.stt_failed", extra={"detail": e.detail})
            raise HTTPException(e.status, e.detail) from None
        return {
            "transcript": result.transcript,
            "confidence": result.confidence,
            "user_text": to_user_text(result.transcript, result.confidence),
        }

    @router.post("/speak")
    async def speak(body: SpeakIn, request: Request) -> Response:
        check_rate(request)
        if sum(len(m) for m in body.messages) > MAX_SPEAK_CHARS:
            raise HTTPException(413, "too much text to speak")
        ssml = to_ssml(body.messages, body.secure_url)
        with cache_lock:
            audio = cache.get(ssml)
            if audio is not None:
                cache.move_to_end(ssml)
        if audio is None:
            tts = speech()
            try:
                audio = await asyncio.to_thread(tts.synthesize, ssml)
            except SpeechError as e:
                log.warning("voice.tts_failed", extra={"detail": e.detail})
                raise HTTPException(e.status, e.detail) from None
            # Turns that carry a booking code or link are unique; caching them only wastes memory.
            if not body.secure_url:
                with cache_lock:
                    cache[ssml] = audio
                    while len(cache) > TTS_CACHE_SIZE:
                        cache.popitem(last=False)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    return router
