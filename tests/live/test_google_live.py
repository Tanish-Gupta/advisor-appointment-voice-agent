"""Live smoke test against the real Google APIs (opt-in).

Run with:  .venv/bin/python -m pytest -m google_live tests/live -q
Needs AGENT_GOOGLE_PREBOOKING_DOC_ID, AGENT_GOOGLE_ADVISOR_EMAIL and a token from
`python -m advisor_agent.mcp_server.google_auth` (or a service account).
It creates one tentative hold far in the future and deletes it again; the Docs line and the
Gmail draft are left behind for manual inspection (code NL-Z999).
"""

import pytest

from advisor_agent.config import Settings
from advisor_agent.mcp_client.client import ToolClient

pytestmark = pytest.mark.google_live

CODE = "NL-Z999"
TOPIC = "Statements/Tax Docs"
START = "2030-01-07T11:00:00+05:30"
END = "2030-01-07T11:30:00+05:30"


@pytest.fixture
def client() -> ToolClient:
    settings = Settings()
    if not (settings.google_prebooking_doc_id and settings.google_advisor_email):
        pytest.skip("Google live env not configured")
    from advisor_agent.mcp_server.server import build_server_from_settings

    return ToolClient(build_server_from_settings(settings), call_timeout_s=30)


async def test_real_google_round_trip(client: ToolClient) -> None:
    busy = await client.call("calendar_list_busy", {"start_ist": START, "end_ist": END})
    assert "busy" in busy

    hold = await client.call(
        "calendar_create_hold",
        {"code": CODE, "topic": TOPIC, "start_ist": START, "end_ist": END, "version": 99},
    )
    assert hold["event_id"]
    try:
        doc = await client.call(
            "docs_append_prebooking",
            {
                "date": "2030-01-07",
                "topic": TOPIC,
                "slot": "Mon, 7 Jan, 11:00 AM IST",
                "code": CODE,
                "status": "tentative",
            },
        )
        assert doc
        draft = await client.call(
            "gmail_create_draft",
            {
                "template": "booking",
                "code": CODE,
                "topic": TOPIC,
                "slot": "Mon, 7 Jan, 11:00 AM IST",
            },
        )
        assert draft
    finally:
        deleted = await client.call("calendar_delete_hold", {"event_id": hold["event_id"]})
        assert deleted["deleted"] is True
