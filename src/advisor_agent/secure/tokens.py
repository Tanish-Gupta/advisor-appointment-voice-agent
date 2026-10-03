"""Signed, expiring, single-use secure links (LLD 4.4).

URL: `{AGENT_PUBLIC_BASE_URL}/b/{code}?t={payload}.{sig}` where
`payload = base64url(json{"c": code, "jti": id, "exp": unix})` and
`sig = base64url(HMAC-SHA256(secret, payload))[:32]`. The token row (jti) in the store makes
the link single-use; the HMAC makes it unforgeable; `exp` makes it expire.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

log = logging.getLogger(__name__)


class TokenStore(Protocol):
    def insert_token(self, jti: str, booking_code: str, expires_at: float) -> None: ...
    def get_token(self, jti: str) -> object | None: ...  # TokenRow with .used_at
    def mark_token_used(self, jti: str, used_at: float) -> bool: ...


class LinkError(Exception):
    """reason: malformed | bad_signature | expired | used | code_mismatch | unknown"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedLink:
    code: str
    jti: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def resolve_secret(secret: str | None, *, env: str) -> bytes:
    """The configured secret, or (outside prod) an ephemeral one: links then die on restart."""
    if secret:
        return secret.encode()
    if env == "prod":
        raise RuntimeError("AGENT_LINK_HMAC_SECRET is required in prod")
    log.warning("AGENT_LINK_HMAC_SECRET not set; using an ephemeral secret (dev only)")
    return secrets.token_bytes(32)


class SecureLinks:
    def __init__(
        self,
        secret: bytes,
        base_url: str,
        store: TokenStore,
        clock: Callable[[], datetime],
        *,
        ttl_hours: int = 48,
    ) -> None:
        self._secret = secret
        self._base = base_url.rstrip("/")
        self._store = store
        self._clock = clock
        self._ttl = timedelta(hours=ttl_hours)
        self._ttl_hours = ttl_hours

    @property
    def ttl_label(self) -> str:
        return f"{self._ttl_hours} hours"

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())[:32]

    def issue(self, code: str) -> str:
        jti = secrets.token_urlsafe(12)
        exp = int((self._clock() + self._ttl).timestamp())
        self._store.insert_token(jti, code, float(exp))
        claims = json.dumps({"c": code, "jti": jti, "exp": exp}, separators=(",", ":"))
        payload = _b64(claims.encode())
        return f"{self._base}/b/{code}?t={payload}.{self._sign(payload)}"

    def verify(self, code: str, token: str) -> VerifiedLink:
        try:
            payload, sig = token.split(".", 1)
        except (AttributeError, ValueError):
            raise LinkError("malformed") from None
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise LinkError("bad_signature")
        try:
            data = json.loads(_unb64(payload))
            claimed, jti, exp = str(data["c"]), str(data["jti"]), int(data["exp"])
        except (ValueError, KeyError, TypeError):
            raise LinkError("malformed") from None
        if claimed != code:
            raise LinkError("code_mismatch")
        if self._clock().timestamp() > exp:
            raise LinkError("expired")
        row = self._store.get_token(jti)
        if row is None:
            raise LinkError("unknown")
        if getattr(row, "used_at", None) is not None:
            raise LinkError("used")
        return VerifiedLink(code=claimed, jti=jti, expires_at=exp)

    def consume(self, link: VerifiedLink) -> bool:
        """Mark the token used; True only for the first caller (atomic in the store)."""
        return self._store.mark_token_used(link.jti, self._clock().timestamp())

    def csrf_token(self, link: VerifiedLink) -> str:
        return _b64(hmac.new(self._secret, f"csrf:{link.jti}".encode(), hashlib.sha256).digest())

    def check_csrf(self, link: VerifiedLink, value: str) -> bool:
        return hmac.compare_digest(value or "", self.csrf_token(link))
