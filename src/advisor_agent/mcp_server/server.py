"""The FastMCP server: five Google tools, nothing else (LLD 3.9).

Run standalone (the agent connects over stdio or HTTP):
    python -m advisor_agent.mcp_server.server                     # stdio
    python -m advisor_agent.mcp_server.server --transport http --port 8001
or in-process with `AGENT_MCP_SERVER_TARGET=inprocess` (still spoken to over MCP).

Errors are reported as `ToolError("retryable:...")` (timeouts, 408/429/5xx) or
`ToolError("permanent:...")` so the outbox worker knows whether to back off and retry.
"""

import argparse
import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from pydantic import Field

from advisor_agent.domain.codes import CODE_RE
from advisor_agent.domain.models import Topic
from advisor_agent.guardrails import pii
from advisor_agent.mcp_server.google_calendar import GoogleCalendar
from advisor_agent.mcp_server.google_docs import GoogleDocs
from advisor_agent.mcp_server.google_gmail import GoogleGmail

T = TypeVar("T")

IstDateTime = Annotated[
    str,
    Field(
        description="ISO-8601 datetime with the IST offset, e.g. 2026-10-06T13:00:00+05:30",
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?\+05:30$",
    ),
]
Code = Annotated[
    str, Field(description="Booking code exactly as given, e.g. NL-A742", pattern=CODE_RE.pattern)
]
TopicName = Literal[
    "KYC/Onboarding",
    "SIP/Mandates",
    "Statements/Tax Docs",
    "Withdrawals & Timelines",
    "Account Changes/Nominee",
]
assert set(TopicName.__args__) == {t.value for t in Topic}  # type: ignore[attr-defined]


def _http_status(e: BaseException) -> int | None:
    resp = getattr(e, "resp", None)
    status = getattr(resp, "status", None)
    return int(status) if status is not None else None


def _check_no_pii(args: dict[str, Any]) -> None:
    from fastmcp.exceptions import ToolError

    for value in args.values():
        if isinstance(value, str) and pii.contains_pii(value):
            raise ToolError("permanent:pii - personal data is not accepted by these tools")


async def _run(fn: Callable[[], T]) -> T:
    """Run a blocking Google client call off the event loop and classify its errors."""
    from fastmcp.exceptions import ToolError

    try:
        return await asyncio.to_thread(fn)
    except ToolError:
        raise
    except Exception as e:
        status = _http_status(e)
        if status is not None:
            kind = "retryable" if status in (408, 429) or status >= 500 else "permanent"
            raise ToolError(f"{kind}:{status} google api error") from e
        if isinstance(e, TimeoutError | OSError):
            raise ToolError(f"retryable:{type(e).__name__}") from e
        if isinstance(e, ValueError):
            raise ToolError(f"permanent:{e}") from e
        raise ToolError(f"retryable:{type(e).__name__}") from e


def build_server(calendar: GoogleCalendar, docs: GoogleDocs, gmail: GoogleGmail) -> Any:
    from fastmcp import FastMCP

    mcp = FastMCP(
        name="advisor-google-tools",
        instructions=(
            "Back-office tools for tentative advisor pre-bookings. Calendar holds are "
            "tentative; email is only ever drafted, never sent."
        ),
    )

    @mcp.tool()
    async def calendar_list_busy(start_ist: IstDateTime, end_ist: IstDateTime) -> dict[str, Any]:
        """Return the busy intervals of the advisor calendar between start and end (IST)."""
        _check_no_pii({"start_ist": start_ist, "end_ist": end_ist})
        busy = await _run(lambda: calendar.list_busy(start_ist, end_ist))
        return {"busy": busy}

    @mcp.tool()
    async def calendar_create_hold(
        code: Code,
        topic: TopicName,
        start_ist: IstDateTime,
        end_ist: IstDateTime,
        kind: Literal["booking", "waitlist"] = "booking",
        version: int = 1,
    ) -> dict[str, Any]:
        """Create the tentative hold 'Advisor Q&A - {topic} - {code}' on the advisor calendar."""
        _check_no_pii({"code": code, "topic": topic, "start_ist": start_ist, "end_ist": end_ist})
        return await _run(
            lambda: calendar.create_hold(
                code=code,
                topic=topic,
                start_ist=start_ist,
                end_ist=end_ist,
                kind=kind,
                version=version,
            )
        )

    @mcp.tool()
    async def calendar_delete_hold(
        event_id: Annotated[
            str, Field(description="Calendar event id of the hold", pattern=r"^[a-v0-9]{5,1024}$")
        ],
    ) -> dict[str, Any]:
        """Release a tentative hold (idempotent: an already deleted hold counts as deleted)."""
        _check_no_pii({"event_id": event_id})
        deleted = await _run(lambda: calendar.delete_hold(event_id))
        return {"deleted": deleted}

    @mcp.tool()
    async def docs_append_prebooking(
        date: Annotated[
            str, Field(description="Booking date YYYY-MM-DD (IST)", pattern=r"^\d{4}-\d{2}-\d{2}$")
        ],
        topic: TopicName,
        slot: Annotated[
            str,
            Field(description="Human-readable slot, e.g. 'Tue, 6 Oct, 1:00 PM IST'", max_length=80),
        ],
        code: Code,
        status: Literal["tentative", "waitlist", "rescheduled", "cancelled", "hold_failed"],
    ) -> dict[str, Any]:
        """Append '{date} | {topic} | {slot} | {code} | {status}' to 'Advisor Pre-Bookings'."""
        _check_no_pii({"date": date, "topic": topic, "slot": slot, "code": code})
        return await _run(
            lambda: docs.append_prebooking(
                date=date, topic=topic, slot=slot, code=code, status=status
            )
        )

    @mcp.tool()
    async def gmail_create_draft(
        template: Literal["booking", "reschedule", "cancel", "waitlist", "ops_alert"],
        code: Code,
        topic: TopicName,
        slot: Annotated[
            str | None, Field(description="Human-readable slot or preference", max_length=80)
        ] = None,
    ) -> dict[str, Any]:
        """Create (never send) the advisor email draft from a fixed template."""
        _check_no_pii({"template": template, "code": code, "topic": topic, "slot": slot})
        return await _run(
            lambda: gmail.create_draft(template=template, code=code, topic=topic, slot=slot)
        )

    return mcp


class GoogleConfigError(RuntimeError):
    pass


def build_server_from_settings(settings: Any) -> Any:
    from advisor_agent.mcp_server.google_auth import build_services, load_credentials

    if not settings.google_prebooking_doc_id:
        raise GoogleConfigError("AGENT_GOOGLE_PREBOOKING_DOC_ID is not set")
    if not settings.google_advisor_email:
        raise GoogleConfigError("AGENT_GOOGLE_ADVISOR_EMAIL is not set")
    creds = load_credentials(
        token_file=settings.google_token_file,
        service_account_file=settings.google_service_account_file,
        delegated_user=settings.google_delegated_user,
    )
    cal_svc, docs_svc, gmail_svc = build_services(creds)
    return build_server(
        GoogleCalendar(cal_svc, settings.google_calendar_id),
        GoogleDocs(docs_svc, settings.google_prebooking_doc_id),
        GoogleGmail(gmail_svc, settings.google_advisor_email),
    )


def main(argv: list[str] | None = None) -> None:
    from advisor_agent.config import get_settings

    parser = argparse.ArgumentParser(description="Advisor Google tools (FastMCP server)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)

    mcp = build_server_from_settings(get_settings())
    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
