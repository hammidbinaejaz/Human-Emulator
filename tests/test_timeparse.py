from datetime import datetime

import pytest

from timeparse import parse_day, parse_when

NOW = datetime(2026, 11, 2, 20, 0)  # Monday


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("tomorrow 9am", datetime(2026, 11, 3, 9, 0)),
        ("Thursday 4pm", datetime(2026, 11, 5, 16, 0)),
        ("thu 16:30", datetime(2026, 11, 5, 16, 30)),
        ("next friday at 10", datetime(2026, 11, 6, 10, 0)),
        ("monday 9am", datetime(2026, 11, 9, 9, 0)),
        ("in 25 minutes", datetime(2026, 11, 2, 20, 25)),
        ("in 2 hours", datetime(2026, 11, 2, 22, 0)),
        ("in 3 days at 2pm", datetime(2026, 11, 5, 14, 0)),
        ("Nov 5 at 4", datetime(2026, 11, 5, 16, 0)),
        ("5th of December 11:15 am", datetime(2026, 12, 5, 11, 15)),
        ("2026-11-20 08:45", datetime(2026, 11, 20, 8, 45)),
        ("2026-11-20T08:45", datetime(2026, 11, 20, 8, 45)),
        ("12am tomorrow", datetime(2026, 11, 3, 0, 0)),
        ("tonight", datetime(2026, 11, 2, 20, 0)),
        ("tomorrow evening", datetime(2026, 11, 3, 18, 0)),
        ("9am", datetime(2026, 11, 3, 9, 0)),
        ("Jan 3", datetime(2027, 1, 3, 9, 0)),
    ],
)
def test_parse_when(phrase, expected):
    assert parse_when(phrase, NOW).value == expected


def test_parse_when_reports_missing_time():
    assert parse_when("Friday", NOW).has_time is False
    assert parse_when("Friday 3pm", NOW).has_time is True


@pytest.mark.parametrize("phrase", ["", "whenever", "{{step2}}"])
def test_parse_when_rejects_nonsense(phrase):
    with pytest.raises(ValueError):
        parse_when(phrase, NOW)


def test_parse_day():
    assert parse_day("today", NOW).isoformat() == "2026-11-02"
    assert parse_day("tomorrow", NOW).isoformat() == "2026-11-03"
    assert parse_day("Sunday", NOW).isoformat() == "2026-11-08"
