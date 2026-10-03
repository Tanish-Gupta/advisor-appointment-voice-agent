"""Contract tests for the FastMCP server, spoken to over the MCP protocol by our ToolClient."""

import pytest

from advisor_agent.domain.plan import ALL_TOOLS
from advisor_agent.mcp_client.client import ToolCallError, ToolClient
from tests.fake_google import FakeGoogle

START = "2026-10-06T13:00:00+05:30"
END = "2026-10-06T13:30:00+05:30"
HOLD = {"code": "NL-A742", "topic": "SIP/Mandates", "start_ist": START, "end_ist": END}


@pytest.fixture
def g() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def client(g: FakeGoogle) -> ToolClient:
    return ToolClient(g.server(), call_timeout_s=5)


async def test_exposes_exactly_the_five_tools(client: ToolClient) -> None:
    decls = await client.declarations()
    names = {d["name"] for d in decls}
    assert names == set(ALL_TOOLS)
    assert not any("send" in n for n in names)
    hold = next(d for d in decls if d["name"] == "calendar_create_hold")
    assert set(hold["parameters"]["required"]) == {"code", "topic", "start_ist", "end_ist"}


async def test_create_hold_through_mcp(client: ToolClient, g: FakeGoogle) -> None:
    res = await client.call("calendar_create_hold", HOLD)
    assert res["created"] is True and res["event_id"]
    again = await client.call("calendar_create_hold", HOLD)
    assert again["event_id"] == res["event_id"] and again["created"] is False
    assert len(g.calendar.live_events()) == 1


async def test_list_busy_through_mcp(client: ToolClient, g: FakeGoogle) -> None:
    await client.call("calendar_create_hold", HOLD)
    res = await client.call("calendar_list_busy", {"start_ist": START, "end_ist": END})
    assert res == {"busy": [{"start": START, "end": END}]}


async def test_docs_and_gmail_through_mcp(client: ToolClient, g: FakeGoogle) -> None:
    await client.call(
        "docs_append_prebooking",
        {
            "date": "2026-10-02",
            "topic": "SIP/Mandates",
            "slot": "Tue, 6 Oct, 1:00 PM IST",
            "code": "NL-A742",
            "status": "tentative",
        },  # fmt: skip
    )
    res = await client.call(
        "gmail_create_draft", {"template": "booking", "code": "NL-A742", "topic": "SIP/Mandates"}
    )
    assert res["draft_id"] == "draft-1"
    assert g.docs.lines() == [
        "2026-10-02 | SIP/Mandates | Tue, 6 Oct, 1:00 PM IST | NL-A742 | tentative"
    ]


@pytest.mark.parametrize(
    "args",
    [
        {**HOLD, "code": "A742"},  # bad code format
        {**HOLD, "topic": "Crypto tips"},  # not a known topic
        {**HOLD, "start_ist": "2026-10-06T13:00:00"},  # no offset
    ],
)
async def test_schema_rejects_bad_args(client: ToolClient, args: dict) -> None:
    with pytest.raises(ToolCallError) as e:
        await client.call("calendar_create_hold", args)
    assert e.value.retryable is False


async def test_pii_rejected_by_server(client: ToolClient, g: FakeGoogle) -> None:
    with pytest.raises(ToolCallError) as e:
        await client.call(
            "gmail_create_draft",
            {
                "template": "booking",
                "code": "NL-A742",
                "topic": "SIP/Mandates",
                "slot": "call me on +91 98765 43210",
            },  # fmt: skip
        )
    assert "pii" in str(e.value) and e.value.retryable is False
    assert g.gmail.drafts_created == []


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (429, True), (403, False)])
async def test_google_errors_map_to_retryable(
    client: ToolClient, g: FakeGoogle, status: int, retryable: bool
) -> None:
    g.calendar.fail = [status]
    with pytest.raises(ToolCallError) as e:
        await client.call("calendar_create_hold", HOLD)
    assert e.value.retryable is retryable


async def test_unknown_tool_is_an_error(client: ToolClient) -> None:
    with pytest.raises(ToolCallError):
        await client.call("gmail_send", {"code": "NL-A742"})
