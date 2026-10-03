import io
import wave

import pytest
from fastapi.testclient import TestClient

from advisor_agent.channels.chat.api import create_app
from advisor_agent.channels.voice.google_speech import SpeechError, SttResult
from tests.conftest import make_settings


def wav_bytes(seconds: float = 1.0, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buf.getvalue()


class FakeSpeech:
    name = "google"
    voice = "en-IN-Neural2-A"

    def __init__(self, transcript: str = "two thirty p.m.", fail: SpeechError | None = None):
        self.transcript = transcript
        self.fail = fail
        self.stt_calls: list[int] = []
        self.tts_calls: list[str] = []

    def transcribe(self, wav: bytes, sample_rate: int) -> SttResult:
        if self.fail:
            raise self.fail
        self.stt_calls.append(sample_rate)
        return SttResult(self.transcript, 0.9)

    def synthesize(self, ssml: str) -> bytes:
        if self.fail:
            raise self.fail
        self.tts_calls.append(ssml)
        return b"ID3mp3"


@pytest.fixture
def fake():
    return FakeSpeech()


@pytest.fixture
def client(service, fake):
    settings = make_settings(voice_provider="google", voice_requests_per_min_per_ip=100)
    return TestClient(create_app(service, settings, speech=lambda: fake))


def test_config_reports_google(client):
    body = client.get("/v1/voice/config").json()
    assert body["provider"] == "google"
    assert body["language"] == "en-IN"
    assert body["voice"] == "en-IN-Neural2-A"
    assert body["reason"] is None


def test_transcribe_returns_user_text(client, fake):
    headers = {"content-type": "audio/wav"}
    res = client.post("/v1/voice/transcribe", content=wav_bytes(), headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body == {"transcript": "two thirty p.m.", "confidence": 0.9, "user_text": "2:30 pm"}
    assert fake.stt_calls == [16000]


def test_transcribe_body_is_allowed_past_the_chat_cap(client):
    big = wav_bytes(seconds=3)
    assert len(big) > 2048
    assert client.post("/v1/voice/transcribe", content=big).status_code == 200


@pytest.mark.parametrize(
    "audio",
    [b"not a wav at all", wav_bytes(channels=2), wav_bytes(width=1), wav_bytes(rate=11025)],
)
def test_transcribe_rejects_bad_audio(client, audio):
    assert client.post("/v1/voice/transcribe", content=audio).status_code == 415


def test_transcribe_rejects_long_utterance(client):
    assert client.post("/v1/voice/transcribe", content=wav_bytes(seconds=21)).status_code == 413


def test_speak_returns_mp3_and_caches(client, fake):
    payload = {"messages": ["Hi! I'm informational, not investment advice.", "What's it about?"]}
    first = client.post("/v1/voice/speak", json=payload)
    assert first.status_code == 200
    assert first.headers["content-type"] == "audio/mpeg"
    assert first.content == b"ID3mp3"
    assert client.post("/v1/voice/speak", json=payload).status_code == 200
    assert len(fake.tts_calls) == 1
    assert fake.tts_calls[0].startswith("<speak>")


def test_speak_with_secure_link_is_not_cached(client, fake):
    url = "https://advisor.example/s/x"
    payload = {"messages": [f"Use this secure link: {url}"], "secure_url": url}
    client.post("/v1/voice/speak", json=payload)
    client.post("/v1/voice/speak", json=payload)
    assert len(fake.tts_calls) == 2
    assert "http" not in fake.tts_calls[0]


@pytest.mark.parametrize(
    "payload",
    [{"messages": []}, {"messages": ["x"] * 11}, {"messages": ["a" * 1500, "b" * 1500]}],
)
def test_speak_validates_size(client, payload):
    assert client.post("/v1/voice/speak", json=payload).status_code in (413, 422)


def test_backend_error_maps_to_status(service):
    fake = FakeSpeech(fail=SpeechError("The Text-to-Speech API is not enabled.", status=503))
    settings = make_settings(voice_provider="google")
    client = TestClient(create_app(service, settings, speech=lambda: fake))
    res = client.post("/v1/voice/speak", json={"messages": ["Hello"]})
    assert res.status_code == 503
    assert "not enabled" in res.json()["detail"]


def test_missing_credentials_falls_back_to_browser(service):
    settings = make_settings(voice_provider="google", google_service_account_file=None)
    client = TestClient(create_app(service, settings))
    body = client.get("/v1/voice/config").json()
    assert body["provider"] == "browser"
    assert "AGENT_GOOGLE_SERVICE_ACCOUNT_FILE" in body["reason"]
    assert client.post("/v1/voice/speak", json={"messages": ["Hi"]}).status_code == 503


def test_browser_provider_never_builds_google(service):
    client = TestClient(create_app(service, make_settings(voice_provider="browser")))
    assert client.get("/v1/voice/config").json()["provider"] == "browser"
    assert client.post("/v1/voice/transcribe", content=wav_bytes()).status_code == 503


def test_rate_limit(service, fake):
    settings = make_settings(voice_provider="google", voice_requests_per_min_per_ip=2)
    client = TestClient(create_app(service, settings, speech=lambda: fake))
    codes = [
        client.post("/v1/voice/speak", json={"messages": [f"m{i}"]}).status_code for i in range(3)
    ]
    assert codes == [200, 200, 429]


def test_chat_body_cap_unchanged(client):
    res = client.post(
        "/v1/sessions/abc/messages",
        content=b"x" * 3000,
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 413
