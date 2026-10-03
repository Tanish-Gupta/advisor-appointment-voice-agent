import pytest
from fastapi.testclient import TestClient

from advisor_agent.channels.chat.api import create_app
from tests.conftest import make_settings


@pytest.fixture
def client(service):
    return TestClient(create_app(service, make_settings(sessions_per_min_per_ip=3,
                                                        messages_per_min_per_session=5)))


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "backend": "memory", "nlu": "stub"}


def test_create_and_message(client):
    res = client.post("/v1/sessions")
    assert res.status_code == 201
    body = res.json()
    assert body["state"] == "disclaimer_ack" and body["quick_replies"] == ["yes", "no"]
    res = client.post(f"/v1/sessions/{body['session_id']}/messages", json={"user_text": "yes"})
    assert res.status_code == 200 and res.json()["state"] == "intent_detect"


def test_unknown_session_404(client):
    res = client.post("/v1/sessions/does-not-exist/messages", json={"user_text": "hi"})
    assert res.status_code == 404


@pytest.mark.parametrize("payload", [{"user_text": ""}, {"user_text": "x" * 501}, {}])
def test_validation_422(client, payload):
    sid = client.post("/v1/sessions").json()["session_id"]
    assert client.post(f"/v1/sessions/{sid}/messages", json=payload).status_code == 422


def test_whitespace_only_422(client):
    sid = client.post("/v1/sessions").json()["session_id"]
    res = client.post(f"/v1/sessions/{sid}/messages", json={"user_text": "   "})
    assert res.status_code == 422


def test_body_cap_413(client):
    sid = client.post("/v1/sessions").json()["session_id"]
    res = client.post(
        f"/v1/sessions/{sid}/messages",
        content=b'{"user_text": "' + b"a" * 3000 + b'"}',
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413


def test_session_rate_limit(client):
    codes = [client.post("/v1/sessions").status_code for _ in range(4)]
    assert codes == [201, 201, 201, 429]


def test_message_rate_limit(client):
    sid = client.post("/v1/sessions").json()["session_id"]
    codes = [
        client.post(f"/v1/sessions/{sid}/messages", json={"user_text": "repeat"}).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_dev_session_snapshot(client):
    sid = client.post("/v1/sessions").json()["session_id"]
    snap = client.get(f"/v1/sessions/{sid}").json()
    assert snap["state"] == "disclaimer_ack" and snap["disclaimer_acknowledged"] is False


def test_snapshot_hidden_in_prod(service):
    client = TestClient(create_app(service, make_settings(env="prod")))
    sid = client.post("/v1/sessions").json()["session_id"]
    assert client.get(f"/v1/sessions/{sid}").status_code in {404, 405}


def test_web_ui_served(client):
    res = client.get("/")
    assert res.status_code == 200 and "Advisor Appointment Scheduler" in res.text
    assert "not investment advice" in res.text
    assert client.get("/app.js").status_code == 200
