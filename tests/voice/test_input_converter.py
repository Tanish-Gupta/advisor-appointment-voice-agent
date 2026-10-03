import pytest

from advisor_agent.channels.voice.input_converter import to_user_text


@pytest.mark.parametrize(
    ("transcript", "confidence", "expected"),
    [
        ("", None, None),
        ("   ", 0.9, None),
        ("um", 0.9, None),
        ("uh hmm", None, None),
        ("yes", 0.3, "repeat"),
        ("the second one", 0.4, "repeat"),
        ("book a KYC slot for tomorrow", 0.4, "book a KYC slot for tomorrow"),
        ("yes", 0.95, "yes"),
        ("Tomorrow at 3 p.m.", 0.9, "Tomorrow at 3 pm"),
        ("two thirty p.m. on Monday", 0.9, "2:30 pm on Monday"),
        ("around ten am", None, "around 10 am"),
        ("three o'clock please", None, "3 o'clock please"),
        ("eleven forty five", None, "11:45"),
        ("the second one", 0.9, "the second one"),
        ("one of them", 0.9, "one of them"),
        ("cancel NL-A742", 0.9, "cancel NL-A742"),
        ("my code is N L A seven four two", 0.9, "my code is N L A seven four two NL-A742"),
        ("my number is 98765 43210", 0.9, "my number is 98765 43210"),
    ],
)
def test_rows(transcript, confidence, expected):
    assert to_user_text(transcript, confidence) == expected
