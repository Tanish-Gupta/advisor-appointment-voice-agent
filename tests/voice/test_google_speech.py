import base64

import pytest

from advisor_agent.channels.voice.google_speech import (
    STT_URL,
    TTS_URL,
    GoogleSpeech,
    SpeechError,
    load_google_speech,
)
from tests.conftest import make_settings


class FakeResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> object:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, json))
        return self.responses.pop(0)


def test_transcribe_joins_results_and_takes_min_confidence():
    session = FakeSession(FakeResponse(200, {"results": [
        {"alternatives": [{"transcript": "book a KYC slot", "confidence": 0.92}]},
        {"alternatives": [{"transcript": " tomorrow morning", "confidence": 0.81}]},
    ]}))  # fmt: skip
    result = GoogleSpeech(session).transcribe(b"RIFF", 16000)
    assert result.transcript == "book a KYC slot tomorrow morning"
    assert result.confidence == pytest.approx(0.81)
    url, payload = session.calls[0]
    assert url == STT_URL
    assert payload["config"]["languageCode"] == "en-IN"
    assert payload["config"]["sampleRateHertz"] == 16000
    assert "KYC" in payload["config"]["speechContexts"][0]["phrases"]
    assert base64.b64decode(payload["audio"]["content"]) == b"RIFF"


def test_transcribe_empty_result():
    result = GoogleSpeech(FakeSession(FakeResponse(200, {}))).transcribe(b"x", 16000)
    assert result.transcript == "" and result.confidence is None


def test_model_fallback_on_bad_request():
    session = FakeSession(
        FakeResponse(400, {"error": {"message": "model not supported for language"}}),
        FakeResponse(200, {"results": [{"alternatives": [{"transcript": "yes"}]}]}),
    )
    speech = GoogleSpeech(session, stt_model="latest_short")
    assert speech.transcribe(b"x", 16000).transcript == "yes"
    assert "model" in session.calls[0][1]["config"]
    assert "model" not in session.calls[1][1]["config"]
    assert speech.stt_model is None


def test_disabled_api_is_explained():
    body = {"error": {"message": "Cloud Text-to-Speech API has not been used in project 1"}}
    with pytest.raises(SpeechError) as err:
        GoogleSpeech(FakeSession(FakeResponse(403, body))).synthesize("<speak>hi</speak>")
    assert err.value.status == 503
    assert "not enabled" in err.value.detail


def test_network_failure_is_unavailable():
    class Broken:
        def post(self, *a, **k):
            raise ConnectionError("dns")

    with pytest.raises(SpeechError) as err:
        GoogleSpeech(Broken()).synthesize("<speak>hi</speak>")
    assert err.value.status == 503


def test_synthesize_sends_ssml_and_decodes_mp3():
    audio = base64.b64encode(b"ID3fake").decode()
    session = FakeSession(FakeResponse(200, {"audioContent": audio}))
    speech = GoogleSpeech(session, voice="en-IN-Neural2-A", speaking_rate=1.1)
    assert speech.synthesize("<speak>hi</speak>") == b"ID3fake"
    url, payload = session.calls[0]
    assert url == TTS_URL
    assert payload["input"] == {"ssml": "<speak>hi</speak>"}
    assert payload["voice"]["name"] == "en-IN-Neural2-A"
    assert payload["audioConfig"] == {"audioEncoding": "MP3", "speakingRate": 1.1}


def test_load_requires_service_account():
    with pytest.raises(SpeechError) as err:
        load_google_speech(make_settings(google_service_account_file=None))
    assert err.value.status == 503


def test_load_reports_unreadable_file(tmp_path):
    bad = tmp_path / "sa.json"
    bad.write_text("{}")
    with pytest.raises(SpeechError) as err:
        load_google_speech(make_settings(google_service_account_file=str(bad)))
    assert "could not be read" in err.value.detail
