"""Gmail v1 wrapper: create (never send) the advisor email draft (LLD 3.9).

The recipient is fixed server-side (`AGENT_GOOGLE_ADVISOR_EMAIL`) and the body comes from
fixed templates, so neither Gemini nor a caller can choose recipients or free text. Drafts are
approval-gated: a human reviews and sends them from Gmail.
"""

import base64
from email.message import EmailMessage
from typing import Any

TEMPLATES: dict[str, tuple[str, str]] = {
    "booking": (
        "[Pre-booking] {topic} \u2014 {code}",
        "A client placed a tentative advisor slot.\n\n"
        "Booking code: {code}\nTopic: {topic}\nSlot: {slot}\n\n"
        "A tentative calendar hold has been created. Contact details will arrive via the "
        "secure form, not by email. Please review before confirming with the client.",
    ),
    "reschedule": (
        "[Rescheduled] {topic} \u2014 {code}",
        "A client moved their tentative booking.\n\nBooking code: {code}\nTopic: {topic}\n"
        "New slot: {slot}\n\nThe previous hold was released.",
    ),
    "cancel": (
        "[Cancelled] {topic} \u2014 {code}",
        "A client cancelled their tentative booking.\n\nBooking code: {code}\nTopic: {topic}\n"
        "Slot: {slot}\n\nThe calendar hold was released.",
    ),
    "waitlist": (
        "[Waitlist] {topic} \u2014 {code}",
        "No slot matched the client's preference; they joined the waitlist.\n\n"
        "Waitlist code: {code}\nTopic: {topic}\nPreference: {slot}\n\n"
        "Please offer a slot when one opens.",
    ),
    "ops_alert": (
        "[Action needed] {code}",
        "An automated step failed for booking {code} ({topic}, {slot}).\n\n"
        "The calendar hold could not be created or released after several retries. Please "
        "check the calendar and the Advisor Pre-Bookings document manually.",
    ),
}


def render(template: str, *, code: str, topic: str, slot: str | None) -> tuple[str, str]:
    if template not in TEMPLATES:
        raise ValueError(f"unknown email template {template!r}")
    subject, body = TEMPLATES[template]
    values = {"code": code, "topic": topic, "slot": slot or "n/a"}
    return subject.format(**values), body.format(**values)


class GoogleGmail:
    def __init__(self, service: Any, advisor_email: str) -> None:
        self._svc = service
        self._to = advisor_email

    def create_draft(
        self, *, template: str, code: str, topic: str, slot: str | None = None
    ) -> dict[str, Any]:
        subject, body = render(template, code=code, topic=topic, slot=slot)
        msg = EmailMessage()
        msg["To"] = self._to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = (
            self._svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        )
        return {"draft_id": draft.get("id"), "subject": subject}
