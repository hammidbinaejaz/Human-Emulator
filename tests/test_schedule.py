from datetime import datetime

import pytest

from schedule import Schedule, extract

# Sunday 8 Nov 2026, 10:00
NOW = datetime(2026, 11, 8, 10, 0)


@pytest.mark.parametrize(
    "goal, kind, at, days, minutes, rest",
    [
        ("Every day at 9 AM, tell me what's on my calendar out loud.", "daily", "09:00", (), 0, "Tell me what's on my calendar out loud."),
        ("every morning brief my day", "daily", "09:00", (), 0, "Brief my day"),
        ("Every night at 11pm switch to dark mode", "daily", "23:00", (), 0, "Switch to dark mode"),
        ("every evening at 7 set volume to 10", "daily", "19:00", (), 0, "Set volume to 10"),
        ("At 8:30 every weekday, read my agenda aloud", "weekdays", "08:30", (), 0, "Read my agenda aloud"),
        ("every Monday and Thursday at 6pm remind me to go to the gym", "weekly", "18:00", (0, 3), 0, "Remind me to go to the gym"),
        ("on Fridays at 5:15 pm write a weekly log file", "weekly", "17:15", (4,), 0, "Write a weekly log file"),
        ("every 2 hours remind me to drink water", "interval", "09:00", (), 120, "Remind me to drink water"),
        ("Check my Mac health every hour", "interval", "09:00", (), 60, "Check my Mac health"),
        ("every minute say hello", "interval", "09:00", (), 5, "Say hello"),
    ],
)
def test_extracts_schedule_and_leaves_the_task(goal, kind, at, days, minutes, rest):
    schedule, remaining = extract(goal)
    assert schedule == Schedule(kind, at=at, days=days, minutes=minutes)
    assert remaining == rest


@pytest.mark.parametrize(
    "goal",
    [
        "Remind me on Monday at 9am to submit the report",
        "Create 3 notes about my viva",
        "Remind me in 25 minutes to take a break",
        "Make a folder for every month of the year",
        "Write my daily standup notes to a file on the desktop",
        "open my everyday notes folder in VS Code",
        "Create a Pages report about my weekdays routine",
        "WhatsApp Sam I will see you every morning",
        "Create 3 notes every day",
        'WhatsApp Mom "call me every evening at 7"',
    ],
)
def test_one_off_goals_are_not_routines(goal):
    assert extract(goal) == (None, goal)


def test_schedule_tied_to_a_time_counts_anywhere():
    schedule, rest = extract("Create 3 notes every day at 9am")
    assert schedule == Schedule("daily", at="09:00")
    assert rest == "Create 3 notes"
    schedule, rest = extract("Please, every weekday read my agenda aloud")
    assert schedule == Schedule("weekdays", at="09:00")
    assert rest == "Read my agenda aloud"


def test_next_runs():
    assert Schedule("daily", at="09:00").next_after(NOW) == datetime(2026, 11, 9, 9, 0)
    assert Schedule("daily", at="18:30").next_after(NOW) == datetime(2026, 11, 8, 18, 30)
    assert Schedule("weekdays", at="08:30").next_after(NOW) == datetime(2026, 11, 9, 8, 30)  # Sunday → Monday
    assert Schedule("weekly", at="18:00", days=(3,)).next_after(NOW) == datetime(2026, 11, 12, 18, 0)
    assert Schedule("interval", minutes=90).next_after(NOW) == datetime(2026, 11, 8, 11, 30)


def test_describe_and_round_trip():
    weekly = Schedule("weekly", at="18:00", days=(0, 3))
    assert weekly.describe() == "Every Monday and Thursday at 6:00 PM"
    assert Schedule.from_dict(weekly.to_dict()) == weekly
    assert Schedule("interval", minutes=120).describe() == "Every 2 hours"
    assert Schedule("weekdays", at="08:30").describe() == "Every weekday at 8:30 AM"
