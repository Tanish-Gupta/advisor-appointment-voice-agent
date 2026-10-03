"""Google Calendar v3 wrapper used by the MCP server (LLD 3.9).

The discovery `service` object is injected, so tests pass `build(..., http=HttpMock)`.
Event IDs are deterministic per `{code}:{kind}:{version}`: a retried insert returns 409 and is
treated as success, which makes `calendar_create_hold` idempotent at Google too.
"""

from datetime import datetime
from typing import Any

from advisor_agent.domain.plan import hold_event_id

IST_TZ = "Asia/Kolkata"

deterministic_event_id = hold_event_id  # one implementation, shared with the plans


def hold_title(topic: str, code: str, kind: str = "booking") -> str:
    """'Advisor Q&A — {Topic} — {Code}' (waitlist: 'Advisor Q&A — Waitlist — {Topic} — {Code}')."""
    if kind == "waitlist":
        return f"Advisor Q&A \u2014 Waitlist \u2014 {topic} \u2014 {code}"
    return f"Advisor Q&A \u2014 {topic} \u2014 {code}"


def _status(e: Any) -> int:
    resp = getattr(e, "resp", None)
    return int(getattr(resp, "status", 0) or 0)


class GoogleCalendar:
    def __init__(self, service: Any, calendar_id: str = "primary") -> None:
        self._svc = service
        self._cal = calendar_id

    def list_busy(self, start_ist: str, end_ist: str) -> list[dict[str, str]]:
        body = {
            "timeMin": datetime.fromisoformat(start_ist).isoformat(),
            "timeMax": datetime.fromisoformat(end_ist).isoformat(),
            "timeZone": IST_TZ,
            "items": [{"id": self._cal}],
        }
        resp = self._svc.freebusy().query(body=body).execute()
        cal = (resp.get("calendars") or {}).get(self._cal) or {}
        if cal.get("errors"):
            reason = cal["errors"][0].get("reason", "unknown")
            raise RuntimeError(f"freebusy error for calendar: {reason}")
        return [{"start": b["start"], "end": b["end"]} for b in cal.get("busy", [])]

    def create_hold(
        self, *, code: str, topic: str, start_ist: str, end_ist: str, kind: str, version: int
    ) -> dict[str, Any]:
        from googleapiclient.errors import HttpError

        event_id = deterministic_event_id(code, kind, version)
        body = {
            "id": event_id,
            "summary": hold_title(topic, code, kind),
            "description": (
                f"Tentative pre-booking {code} ({topic}). Created by the advisor scheduling "
                "assistant. Contact details are collected separately via the secure link; "
                "informational only, not investment advice."
            ),
            "start": {"dateTime": start_ist, "timeZone": IST_TZ},
            "end": {"dateTime": end_ist, "timeZone": IST_TZ},
            "status": "tentative",
            # a waitlist hold is a marker, it must not block the advisor's calendar
            "transparency": "transparent" if kind == "waitlist" else "opaque",
            "visibility": "private",
            "extendedProperties": {"private": {"booking_code": code, "kind": kind}},
        }
        try:
            ev = self._svc.events().insert(calendarId=self._cal, body=body).execute()
            return {
                "event_id": ev.get("id", event_id),
                "html_link": ev.get("htmlLink"),
                "created": True,
            }
        except HttpError as e:
            if _status(e) != 409:
                raise
        # 409: an event with this deterministic id exists (a retry). Revive it if it was deleted.
        ev = self._svc.events().get(calendarId=self._cal, eventId=event_id).execute()
        if ev.get("status") == "cancelled":
            ev = (
                self._svc.events()
                .update(calendarId=self._cal, eventId=event_id, body=body)
                .execute()
            )
        return {"event_id": event_id, "html_link": ev.get("htmlLink"), "created": False}

    def delete_hold(self, event_id: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            self._svc.events().delete(
                calendarId=self._cal, eventId=event_id, sendUpdates="none"
            ).execute()
        except HttpError as e:
            if _status(e) in (404, 410):  # already gone: deletion is idempotent
                return True
            raise
        return True
