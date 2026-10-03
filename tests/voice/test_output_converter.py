import pytest

from advisor_agent.channels.voice.output_converter import to_plain_speech, to_ssml
from advisor_agent.orchestrator.content import edu_links_text

URL = "https://advisor.example/s/abc123"


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        (
            "How about Tuesday, 6 October 2026, 2:00 PM IST?",
            "How about Tuesday, the 6th of October, at 2 PM, India Standard Time?",
        ),
        (
            "Or Wednesday, 21 October 2026, 10:30 AM IST.",
            "Or Wednesday, the 21st of October, at 10:30 AM, India Standard Time.",
        ),
        ("All times are in IST.", "All times are in India Standard Time."),
        ("Mornings work (IST).", "Mornings work."),
        ("Shall I book it? (yes / no)", "Shall I book it? Please say yes or no."),
        ("This is **important**.", "This is important."),
    ],
)
def test_plain_speech_rows(text, spoken):
    assert to_plain_speech([text]) == spoken


def test_secure_link_is_never_read_out():
    msg = f"Please add your details through this secure link rather than here in the chat: {URL}"
    spoken = to_plain_speech([msg], secure_url=URL)
    assert "http" not in spoken
    assert spoken.endswith("rather than on this call. I've put the secure link on your screen.")


def test_edu_links_become_names_only():
    spoken = to_plain_speech(["Good places to start: " + edu_links_text()])
    assert "http" not in spoken and ";" not in spoken
    assert spoken.endswith("I've put the links on your screen.")


def test_new_booking_code_is_spelled_and_read_twice():
    ssml = to_ssml(["Your booking code is NL-A742."])
    spelled = '<say-as interpret-as="characters">NLA742</say-as>'
    assert ssml.count(spelled) == 2
    assert "that's" in ssml


def test_code_mentioned_elsewhere_is_read_once():
    ssml = to_ssml(["Your booking NL-A742 is cancelled."])
    assert ssml.count('interpret-as="characters">NLA742') == 1


def test_options_disclaimer_and_abbreviations():
    ssml = to_ssml([
        "Hi! I'm informational, not investment advice.",
        "Two options: 1) Tuesday, 6 October 2026, 2:00 PM IST or 2) Wednesday, 7 October 2026, "
        "11:00 AM IST.",
        "Topic: KYC/Onboarding and SIPs.",
    ])  # fmt: skip
    assert ssml.startswith('<speak><prosody rate="95%">Hi!')
    assert "option one, Tuesday" in ssml and "or option two, Wednesday" in ssml
    assert '<say-as interpret-as="characters">KYC</say-as> and Onboarding' in ssml
    assert '<say-as interpret-as="characters">SIP</say-as>s' in ssml
    assert ssml.count('<break time="300ms"/>') == 2


def test_text_is_xml_escaped():
    ssml = to_ssml(["Statements & <tax> docs"])
    assert "&amp;" in ssml and "&lt;tax&gt;" in ssml and "<tax>" not in ssml


def test_empty_messages_are_skipped():
    assert to_ssml(["", "  ", "Hello"]) == "<speak>Hello</speak>"
