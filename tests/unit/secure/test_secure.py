"""Signed single-use secure links, the PII vault, and the /b/{code} form."""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from advisor_agent.secure.tokens import LinkError, SecureLinks, resolve_secret
from advisor_agent.storage.sqlite import SqliteStore
from tests.conftest import NOW

CODE = "NL-A742"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def links(store: SqliteStore, clock: Clock) -> SecureLinks:
    return SecureLinks(b"k" * 32, "https://book.example.com/", store, clock, ttl_hours=48)


def _token(url: str) -> str:
    return parse_qs(urlsplit(url).query)["t"][0]


def test_issue_format(links: SecureLinks) -> None:
    url = links.issue(CODE)
    assert url.startswith(f"https://book.example.com/b/{CODE}?t=")
    assert links.ttl_label == "48 hours"
    link = links.verify(CODE, _token(url))
    assert link.code == CODE


def test_tampering_and_mismatch(links: SecureLinks) -> None:
    token = _token(links.issue(CODE))
    payload, sig = token.split(".")
    with pytest.raises(LinkError) as e:
        links.verify(CODE, f"{payload}.{'A' * len(sig)}")
    assert e.value.reason == "bad_signature"
    with pytest.raises(LinkError) as e:
        links.verify("NL-B222", token)
    assert e.value.reason == "code_mismatch"
    with pytest.raises(LinkError) as e:
        links.verify(CODE, "garbage")
    assert e.value.reason == "malformed"


def test_other_secret_rejected(links: SecureLinks, store: SqliteStore, clock: Clock) -> None:
    other = SecureLinks(b"z" * 32, "https://x", store, clock)
    with pytest.raises(LinkError) as e:
        other.verify(CODE, _token(links.issue(CODE)))
    assert e.value.reason == "bad_signature"


def test_expiry(links: SecureLinks, clock: Clock) -> None:
    token = _token(links.issue(CODE))
    clock.now += timedelta(hours=48, seconds=1)
    with pytest.raises(LinkError) as e:
        links.verify(CODE, token)
    assert e.value.reason == "expired"


def test_single_use(links: SecureLinks) -> None:
    link = links.verify(CODE, _token(url := links.issue(CODE)))
    assert links.consume(link) is True
    assert links.consume(link) is False
    with pytest.raises(LinkError) as e:
        links.verify(CODE, _token(url))
    assert e.value.reason == "used"


def test_csrf(links: SecureLinks) -> None:
    link = links.verify(CODE, _token(links.issue(CODE)))
    assert links.check_csrf(link, links.csrf_token(link))
    assert not links.check_csrf(link, "nope")
    assert not links.check_csrf(link, "")


def test_resolve_secret() -> None:
    assert len(resolve_secret(None, env="dev")) >= 32  # ephemeral in dev
    assert resolve_secret("s3cret", env="prod") == b"s3cret"
    with pytest.raises(RuntimeError, match="required in prod"):
        resolve_secret(None, env="prod")


# --- vault -------------------------------------------------------------------------------


def test_vault_encrypts_at_rest(store: SqliteStore, clock: Clock) -> None:
    pytest.importorskip("cryptography")
    from advisor_agent.secure.vault import PiiVault, resolve_key

    vault = PiiVault(resolve_key(None, env="dev"), store, clock)
    details = {"name": "Asha", "phone": "+919876543210", "email": "asha@example.com"}
    vault.put(CODE, details)
    raw = store.get_vault(CODE)
    assert raw is not None and b"9876543210" not in raw and b"asha" not in raw
    assert vault.get(CODE) == details
    assert vault.get("NL-B222") is None
    with pytest.raises(RuntimeError, match="required in prod"):
        resolve_key(None, env="prod")


# --- routes ------------------------------------------------------------------------------


@pytest.fixture
def web(links: SecureLinks, store: SqliteStore, clock: Clock):  # type: ignore[no-untyped-def]
    pytest.importorskip("cryptography")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from advisor_agent.secure.routes import build_secure_router
    from advisor_agent.secure.vault import PiiVault, resolve_key

    vault = PiiVault(resolve_key(None, env="dev"), store, clock)
    app = FastAPI()
    app.include_router(build_secure_router(links, vault))
    return TestClient(app), vault


def _form_fields(html: str) -> dict[str, str]:
    import re

    return dict(re.findall(r'name="(t|csrf)" value="([^"]+)"', html))


GOOD = {
    "name": "Asha Rao",
    "phone": "+91 98765 43210",
    "email": "asha@example.com",
    "consent": "on",
}


def test_form_get_and_submit(web, links: SecureLinks, store: SqliteStore) -> None:  # type: ignore[no-untyped-def]
    client, vault = web
    url = links.issue(CODE)
    page = client.get(urlsplit(url).path + "?" + urlsplit(url).query)
    assert page.status_code == 200
    assert page.headers["cache-control"].startswith("no-store")
    hidden = _form_fields(page.text)
    resp = client.post(f"/b/{CODE}", data={**hidden, **GOOD})
    assert resp.status_code == 200 and "Thank you" in resp.text
    assert vault.get(CODE)["phone"] == "+919876543210"
    again = client.post(f"/b/{CODE}", data={**hidden, **GOOD})
    assert again.status_code == 410


def test_validation_errors_keep_link_usable(web, links: SecureLinks) -> None:  # type: ignore[no-untyped-def]
    client, vault = web
    url = links.issue(CODE)
    hidden = _form_fields(client.get(f"/b/{CODE}?{urlsplit(url).query}").text)
    bad = client.post(f"/b/{CODE}", data={**hidden, **GOOD, "phone": "98765", "consent": ""})
    assert bad.status_code == 422 and "international format" in bad.text
    assert vault.get(CODE) is None
    ok = client.post(f"/b/{CODE}", data={**hidden, **GOOD})
    assert ok.status_code == 200


def test_bad_csrf_and_bad_links(web, links: SecureLinks, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    client, _ = web
    url = links.issue(CODE)
    token = _token(url)
    assert client.post(f"/b/{CODE}", data={"t": token, "csrf": "x", **GOOD}).status_code == 400
    assert client.get(f"/b/{CODE}?t=garbage").status_code == 400
    assert client.get(f"/b/NL-B222?t={token}").status_code == 400
    assert client.get("/b/not-a-code?t=x").status_code == 400
    clock.now += timedelta(days=3)
    assert client.get(f"/b/{CODE}?t={token}").status_code == 410
