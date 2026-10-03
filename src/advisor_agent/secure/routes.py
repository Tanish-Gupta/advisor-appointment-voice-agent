"""Secure contact-details form (LLD 4.6): GET/POST /b/{code}?t=...

Server-rendered, no JavaScript. Submitted values are validated, encrypted into the PII vault
and never echoed back, logged, or shown in the chat. Every response is `no-store`.
"""

import html
import re
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from advisor_agent.secure.tokens import LinkError, SecureLinks, VerifiedLink
from advisor_agent.secure.vault import PiiVault

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
CODE_PATH_RE = r"NL-[A-Z][0-9]{3}"

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Advisor booking - contact details</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.8rem 0 .2rem}}input[type=text],input[type=tel],input[type=email]
{{width:100%;padding:.5rem}}.err{{color:#b00020}}button{{margin-top:1rem;padding:.6rem 1.2rem}}
</style></head><body>{body}</body></html>"""

_LINK_MESSAGES = {
    "expired": "This link has expired. Please start a new chat to get a fresh link.",
    "used": "These details were already submitted. Thank you!",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers=_HEADERS)


def _invalid(reason: str) -> HTMLResponse:
    msg = _LINK_MESSAGES.get(reason, "This link is invalid. Please use the link from your chat.")
    status = 410 if reason in ("expired", "used") else 400
    return _page(f"<h1>Secure link</h1><p>{html.escape(msg)}</p>", status)


def _form(code: str, token: str, csrf: str, errors: list[str] | None = None) -> str:
    err = "".join(f'<p class="err">{html.escape(e)}</p>' for e in errors or [])
    return f"""<h1>Booking {html.escape(code)}</h1>
<p>Share the details the advisor needs to reach you. They are encrypted and used only for this
booking. This service is informational and not investment advice.</p>{err}
<form method="post" action="/b/{html.escape(code)}" autocomplete="off">
<input type="hidden" name="t" value="{html.escape(token)}">
<input type="hidden" name="csrf" value="{html.escape(csrf)}">
<label for="name">Full name</label>
<input id="name" name="name" type="text" maxlength="100" required>
<label for="phone">Phone (international format, e.g. +919876543210)</label>
<input id="phone" name="phone" type="tel" maxlength="16" required>
<label for="email">Email</label>
<input id="email" name="email" type="email" maxlength="254" required>
<label><input name="consent" type="checkbox" value="on" required> I agree to be contacted
about this booking.</label>
<button type="submit">Submit securely</button></form>"""


def validate(fields: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    name = fields.get("name", "").strip()
    phone = re.sub(r"[\s-]", "", fields.get("phone", ""))
    email = fields.get("email", "").strip()
    errors = []
    if not 1 <= len(name) <= 100:
        errors.append("Please enter your name (up to 100 characters).")
    if not E164_RE.match(phone):
        errors.append("Please enter the phone number in international format, e.g. +919876543210.")
    if len(email) > 254 or not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address.")
    if fields.get("consent") != "on":
        errors.append("Please tick the consent box.")
    return {"name": name, "phone": phone, "email": email, "consent": True}, errors


def build_secure_router(links: SecureLinks, vault: PiiVault) -> APIRouter:
    router = APIRouter()

    def _verify(code: str, token: str) -> VerifiedLink | HTMLResponse:
        try:
            return links.verify(code, token)
        except LinkError as e:
            return _invalid(e.reason)

    @router.get("/b/{code}", include_in_schema=False)
    async def secure_form(code: str, t: str = "") -> HTMLResponse:
        if not re.fullmatch(CODE_PATH_RE, code):
            return _invalid("malformed")
        link = _verify(code, t)
        if isinstance(link, HTMLResponse):
            return link
        return _page(_form(code, t, links.csrf_token(link)))

    @router.post("/b/{code}", include_in_schema=False)
    async def secure_submit(code: str, request: Request) -> HTMLResponse:
        if not re.fullmatch(CODE_PATH_RE, code):
            return _invalid("malformed")
        raw = (await request.body()).decode("utf-8", errors="replace")
        fields = {k: v[0] for k, v in parse_qs(raw, max_num_fields=10).items()}
        token = fields.get("t", "")
        link = _verify(code, token)
        if isinstance(link, HTMLResponse):
            return link
        if not links.check_csrf(link, fields.get("csrf", "")):
            return _invalid("bad_signature")
        details, errors = validate(fields)
        if errors:
            return _page(_form(code, token, links.csrf_token(link), errors), 422)
        if not links.consume(link):
            return _invalid("used")
        vault.put(code, details)
        return _page(
            "<h1>Thank you</h1><p>Your details were saved securely. An advisor will confirm "
            "your tentative slot. You can close this page.</p>"
        )

    return router
