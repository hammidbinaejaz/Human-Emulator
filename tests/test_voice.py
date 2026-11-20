import pytest

import voice
from schedule import Schedule, extract


@pytest.mark.parametrize(
    "heard, cleaned",
    [
        ("Open my Final Year project in Versus code.", "Open my Final Year project in VS Code."),
        ("send hi to mom on whats app", "send hi to mom on WhatsApp"),
        ("Every weekday at 8.30, send high to Sam on WhatsApp.", "Every weekday at 8.30, send hi to Sam on WhatsApp."),
        ("The high score is great", "The high score is great"),
    ],
)
def test_transcript_fixups(heard, cleaned):
    assert voice.clean_transcript(heard) == cleaned


def test_spoken_routine_with_dotted_time():
    schedule, rest = extract("Every weekday at 8.30, send hi to Sam on WhatsApp.")
    assert schedule == Schedule("weekdays", at="08:30")
    assert rest == "Send hi to Sam on WhatsApp."
