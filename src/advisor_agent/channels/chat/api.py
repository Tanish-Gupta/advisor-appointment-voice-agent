"""HTTP chat API (LLD 2.12): a thin client of ChatService. Also serves the web chat UI."""

import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from advisor_agent.channels.chat.bootstrap import build_chat_service
from advisor_agent.channels.chat.service import (
    MAX_TEXT_LEN,
    ChatReply,
    ChatService,
    InvalidMessage,
    SessionNotFound,
)
from advisor_agent.channels.voice.google_speech import SpeechBackend
from advisor_agent.channels.voice.routes import build_voice_router, max_audio_bytes
from advisor_agent.config import Settings, get_settings

MAX_BODY_BYTES = 2048
SPEAK_BODY_BYTES = 16 * 1024
WEB_DIR = Path(__file__).parent / "web"


class MessageIn(BaseModel):
    user_text: str = Field(min_length=1, max_length=MAX_TEXT_LEN)


class RateLimiter:
    """Fixed 60-second sliding window per key, in memory (Phase 8 moves this to Redis)."""

    def __init__(self, limit_per_min: int) -> None:
        self._limit = limit_per_min
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self._limit:
            return False
        q.append(now)
        return True


def create_app(
    service: ChatService | None = None,
    settings: Settings | None = None,
    speech: Callable[[], SpeechBackend] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    service = service or build_chat_service(settings)
    session_limiter = RateLimiter(settings.sessions_per_min_per_ip)
    message_limiter = RateLimiter(settings.messages_per_min_per_session)

    runtime = service.runtime

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime is not None:
            await runtime.start()
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.stop()

    app = FastAPI(
        title="Advisor Appointment Scheduler — Chat API", version="0.4.0", lifespan=lifespan
    )

    body_caps = {
        "/v1/voice/transcribe": max_audio_bytes(settings),
        "/v1/voice/speak": SPEAK_BODY_BYTES,
    }

    @app.middleware("http")
    async def cap_body(request: Request, call_next):  # type: ignore[no-untyped-def]
        length = request.headers.get("content-length")
        cap = body_caps.get(request.url.path, MAX_BODY_BYTES)
        if length is not None and int(length) > cap:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    @app.post("/v1/sessions", status_code=201, response_model=ChatReply)
    async def create_session(request: Request) -> ChatReply:
        ip = request.client.host if request.client else "unknown"
        if not session_limiter.allow(ip):
            raise HTTPException(429, "too many sessions, please wait a minute")
        return await service.start()

    @app.post("/v1/sessions/{session_id}/messages", response_model=ChatReply)
    async def send_message(session_id: str, body: MessageIn) -> ChatReply:
        if not message_limiter.allow(session_id):
            raise HTTPException(429, "too many messages, please slow down")
        try:
            return await service.send(session_id, body.user_text)
        except SessionNotFound:
            raise HTTPException(404, "session expired or not found; create a new session") from None
        except InvalidMessage as e:
            raise HTTPException(422, str(e)) from None

    if settings.env != "prod":

        @app.get("/v1/sessions/{session_id}")
        async def get_session(session_id: str) -> dict[str, object]:
            try:
                return service.snapshot(session_id)
            except SessionNotFound:
                raise HTTPException(404, "session not found") from None

    if runtime is not None:
        from advisor_agent.secure.routes import build_secure_router

        app.include_router(build_secure_router(runtime.links, runtime.vault))

        if settings.env != "prod":

            @app.get("/admin/outbox")
            async def outbox(code: str) -> dict[str, object]:
                booking = runtime.store.get_booking(code)
                if booking is None:
                    raise HTTPException(404, "unknown booking code")
                return {
                    "booking": booking.model_dump(mode="json"),
                    "jobs": [j.public() for j in runtime.store.jobs(code)],
                }

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        if runtime is None:
            return {"ok": True, "backend": "memory", "nlu": settings.nlu_engine}
        return {"ok": True, "backend": "sqlite", "nlu": settings.nlu_engine, "mcp": True}

    app.include_router(build_voice_router(settings, speech))

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def main() -> None:
    import argparse

    import uvicorn

    from advisor_agent.logging import configure_logging

    parser = argparse.ArgumentParser(description="Run the chat HTTP API + web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.env == "prod")
    uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
