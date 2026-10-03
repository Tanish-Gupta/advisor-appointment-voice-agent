"""Chat-first end-to-end sandbox (Phases 1-6) without Google credentials.

Runs the real chat API + web UI, orchestrator, NLU, MCP client, ToolGate, outbox worker and our
FastMCP server; only Google's HTTP endpoint is replaced by the in-memory FakeGoogle from the
test suite. Inspect what the agent "wrote to Google" at GET /sandbox/google.

    .venv/bin/python scripts/sandbox_api.py [--port 8000] [--nlu rules|hybrid|gemini|stub]
"""

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fake_google import FakeGoogle  # noqa: E402

from advisor_agent.channels.chat.api import create_app  # noqa: E402
from advisor_agent.channels.chat.bootstrap import build_chat_service  # noqa: E402
from advisor_agent.config import get_settings  # noqa: E402


def _body(msg: Any) -> str:
    part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), msg)
    payload = part.get_payload(decode=True)
    return payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload)


def build(nlu: str | None = None, db: str = "sqlite:///:memory:") -> tuple[Any, FakeGoogle]:
    overrides: dict[str, Any] = {"database_url": db, "env": "dev"}
    if nlu:
        overrides["nlu_engine"] = nlu
    settings = get_settings().model_copy(update=overrides)
    google = FakeGoogle()
    service = build_chat_service(settings, tool_target=google.server())
    app = create_app(service=service, settings=settings)

    @app.get("/sandbox/google", include_in_schema=False)
    async def sandbox_google() -> dict[str, object]:
        return {
            "calendar_events": [
                {k: e.get(k) for k in ("id", "summary", "start", "end", "transparency")}
                for e in google.calendar.live_events()
            ],
            "doc_lines": google.docs.lines(),
            "gmail_drafts": [
                {"to": m["To"], "subject": m["Subject"], "body": _body(m)}
                for m in google.gmail.drafts_created
            ],
        }

    # Static web UI is mounted at "/"; move our route ahead of the catch-all mount.
    app.router.routes.insert(0, app.router.routes.pop())
    return app, google


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--nlu", choices=["stub", "rules", "hybrid", "gemini"])
    args = parser.parse_args()
    app, _ = build(args.nlu)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
