"""In-memory stand-ins for the googleapiclient discovery services (Calendar v3, Docs v1,
Gmail v1).

They mimic only the call chains our wrappers use (`svc.events().insert(...).execute()`), so
the real wrappers, the real FastMCP server and the real MCP client all run in tests; only
Google's HTTP endpoint is replaced. `fail` injects HttpErrors (by status) into the next calls.
"""

import base64
import email
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("googleapiclient")
import httplib2  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402


def http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b'{"error": "fake"}')


class _Req:
    def __init__(self, owner: "_Base", fn: Callable[[], Any]) -> None:
        self._owner = owner
        self._fn = fn

    def execute(self) -> Any:
        self._owner.requests += 1
        if self._owner.fail:
            raise http_error(self._owner.fail.pop(0))
        return self._fn()


class _Base:
    def __init__(self) -> None:
        self.fail: list[int] = []
        self.requests = 0


class FakeCalendarService(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.events_by_id: dict[str, dict[str, Any]] = {}
        self.external_busy: list[dict[str, str]] = []  # busy intervals not created by us
        self.last_freebusy_body: dict[str, Any] | None = None

    # freebusy().query(body=...).execute()
    def freebusy(self) -> "FakeCalendarService":
        return self

    def query(self, body: dict[str, Any]) -> _Req:
        self.last_freebusy_body = body

        def run() -> dict[str, Any]:
            busy = list(self.external_busy) + [
                {"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
                for e in self.events_by_id.values()
                if e.get("status") != "cancelled"
            ]
            return {"calendars": {body["items"][0]["id"]: {"busy": busy}}}

        return _Req(self, run)

    def events(self) -> "_Events":
        return _Events(self)

    def live_events(self) -> list[dict[str, Any]]:
        return [e for e in self.events_by_id.values() if e.get("status") != "cancelled"]


class _Events:
    def __init__(self, cal: FakeCalendarService) -> None:
        self._cal = cal

    def insert(self, calendarId: str, body: dict[str, Any]) -> _Req:  # noqa: N803
        def run() -> dict[str, Any]:
            if body["id"] in self._cal.events_by_id:
                raise http_error(409)
            ev = {**body, "htmlLink": f"https://calendar.example/{body['id']}"}
            self._cal.events_by_id[body["id"]] = ev
            return ev

        return _Req(self._cal, run)

    def get(self, calendarId: str, eventId: str) -> _Req:  # noqa: N803
        def run() -> dict[str, Any]:
            if eventId not in self._cal.events_by_id:
                raise http_error(404)
            return self._cal.events_by_id[eventId]

        return _Req(self._cal, run)

    def update(self, calendarId: str, eventId: str, body: dict[str, Any]) -> _Req:  # noqa: N803
        def run() -> dict[str, Any]:
            self._cal.events_by_id[eventId] = {
                **body,
                "htmlLink": f"https://calendar.example/{eventId}",
            }
            return self._cal.events_by_id[eventId]

        return _Req(self._cal, run)

    def delete(self, calendarId: str, eventId: str, sendUpdates: str = "none") -> _Req:  # noqa: N803
        def run() -> str:
            ev = self._cal.events_by_id.get(eventId)
            if ev is None or ev.get("status") == "cancelled":
                raise http_error(410)
            ev["status"] = "cancelled"
            return ""

        return _Req(self._cal, run)


class FakeDocsService(_Base):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.text = text  # document body without the trailing newline

    def documents(self) -> "FakeDocsService":
        return self

    def get(self, documentId: str) -> _Req:  # noqa: N803
        def run() -> dict[str, Any]:
            return {
                "body": {
                    "content": [
                        {"endIndex": 1, "sectionBreak": {}},
                        {
                            "endIndex": len(self.text) + 2,
                            "paragraph": {"elements": [{"textRun": {"content": self.text + "\n"}}]},
                        },
                    ]
                }
            }

        return _Req(self, run)

    def batchUpdate(self, documentId: str, body: dict[str, Any]) -> _Req:  # noqa: N802, N803
        def run() -> dict[str, Any]:
            for req in body["requests"]:
                ins = req["insertText"]
                pos = ins["location"]["index"] - 1
                self.text = self.text[:pos] + ins["text"] + self.text[pos:]
            return {}

        return _Req(self, run)

    def lines(self) -> list[str]:
        return [ln for ln in self.text.splitlines() if ln]


class FakeGmailService(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.drafts_created: list[email.message.Message] = []
        self.sent = 0  # there is no send endpoint; this stays 0

    def users(self) -> "FakeGmailService":
        return self

    def drafts(self) -> "FakeGmailService":
        return self

    def create(self, userId: str, body: dict[str, Any]) -> _Req:  # noqa: N803
        def run() -> dict[str, Any]:
            raw = base64.urlsafe_b64decode(body["message"]["raw"])
            self.drafts_created.append(email.message_from_bytes(raw))
            return {"id": f"draft-{len(self.drafts_created)}"}

        return _Req(self, run)


class FakeGoogle:
    """The three services plus the real wrappers and the real FastMCP server built on them."""

    def __init__(self, advisor_email: str = "advisor@example.com") -> None:
        from advisor_agent.mcp_server.google_calendar import GoogleCalendar
        from advisor_agent.mcp_server.google_docs import GoogleDocs
        from advisor_agent.mcp_server.google_gmail import GoogleGmail

        self.calendar = FakeCalendarService()
        self.docs = FakeDocsService()
        self.gmail = FakeGmailService()
        self.cal_api = GoogleCalendar(self.calendar, "primary")
        self.docs_api = GoogleDocs(self.docs, "doc-123")
        self.gmail_api = GoogleGmail(self.gmail, advisor_email)

    def server(self) -> Any:
        from advisor_agent.mcp_server.server import build_server

        return build_server(self.cal_api, self.docs_api, self.gmail_api)
