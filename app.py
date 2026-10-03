"""Vercel entrypoint: exposes the FastAPI `app`. Locally, keep using `agent-api`."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if os.environ.get("VERCEL"):
    # Only /tmp is writable on Vercel, and secrets/ is not deployed: the service-account JSON
    # comes from the AGENT_GOOGLE_SERVICE_ACCOUNT_JSON env var instead.
    os.environ.setdefault("AGENT_DATABASE_URL", "sqlite:////tmp/agent.db")
    sa_json = os.environ.get("AGENT_GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json and not os.environ.get("AGENT_GOOGLE_SERVICE_ACCOUNT_FILE"):
        sa_path = Path("/tmp/google-service-account.json")
        sa_path.write_text(sa_json)
        sa_path.chmod(0o600)
        os.environ["AGENT_GOOGLE_SERVICE_ACCOUNT_FILE"] = str(sa_path)

from advisor_agent.channels.chat.api import create_app  # noqa: E402
from advisor_agent.config import get_settings  # noqa: E402
from advisor_agent.logging import configure_logging  # noqa: E402

settings = get_settings()
configure_logging(settings.log_level, json_output=settings.env == "prod")
app = create_app(settings=settings)
