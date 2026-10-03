"""PII vault (LLD 4.5): contact details from the secure form, encrypted at rest with Fernet.

Nothing from the vault is ever sent to Gemini, the MCP tools, logs or the chat. `cryptography`
is imported lazily.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)


class VaultStore(Protocol):
    def put_vault(self, booking_code: str, ciphertext: bytes, now: datetime) -> None: ...
    def get_vault(self, booking_code: str) -> bytes | None: ...


def resolve_key(key: str | None, *, env: str) -> bytes:
    if key:
        return key.encode()
    if env == "prod":
        raise RuntimeError("AGENT_VAULT_FERNET_KEY is required in prod")
    from cryptography.fernet import Fernet

    log.warning("AGENT_VAULT_FERNET_KEY not set; using an ephemeral key (dev only)")
    return Fernet.generate_key()


class PiiVault:
    def __init__(self, key: bytes, store: VaultStore, clock: Callable[[], datetime]) -> None:
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key)
        self._store = store
        self._clock = clock

    def put(self, booking_code: str, details: dict[str, Any]) -> None:
        token = self._fernet.encrypt(json.dumps(details, sort_keys=True).encode())
        self._store.put_vault(booking_code, token, self._clock())

    def get(self, booking_code: str) -> dict[str, Any] | None:
        token = self._store.get_vault(booking_code)
        if token is None:
            return None
        data: dict[str, Any] = json.loads(self._fernet.decrypt(token))
        return data
