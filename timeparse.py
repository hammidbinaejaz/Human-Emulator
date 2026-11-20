"""Deterministic natural-language time parsing.

The planner model copies the user's own wording ("tomorrow 9am", "Thursday
4:30pm", "in 25 minutes"). Turning that into a real date is done here, in
plain code, because small on-device models are unreliable at date arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

DAYPARTS = {
    "morning": time(9, 0),
    "noon": time(12, 0),
    "midday": time(12, 0),
    "afternoon": time(14, 0),
    "evening": time(18, 0),
    "tonight": time(20, 0),
    "night": time(21, 0),
    "midnight": time(0, 0),
}

DEFAULT_TIME = time(9, 0)

_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY_NAMES = "|".join(sorted(WEEKDAYS, key=len, reverse=True))


@dataclass(frozen=True)
class ParsedWhen:
    value: datetime
    has_time: bool


def _parse_clock(text: str) -> tuple[time | None, str]:
    """Find a clock time and return it with the matched text removed."""
    patterns = [
        # 9am, 9:30 pm, 9.30pm, 12 a.m.
        r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?=\W|$)",
        # 16:00, 9:30
        r"\b(\d{1,2})[:.](\d{2})\b()",
        # at 4, at 16
        r"\bat\s+(\d{1,2})\b()()",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").replace(".", "")
        if minute > 59 or hour > 23:
            continue
        if meridiem:
            if hour > 12 or hour == 0:
                continue
            if meridiem.startswith("p") and hour != 12:
                hour += 12
            if meridiem.startswith("a") and hour == 12:
                hour = 0
        elif index == 2 and 1 <= hour <= 7:
            # "at 4" almost always means the afternoon.
            hour += 12
        remaining = (text[: match.start()] + " " + text[match.end():]).strip()
        return time(hour, minute), remaining
    for word, value in DAYPARTS.items():
        if re.search(rf"\b{word}\b", text):
            return value, text
    return None, text


def _next_weekday(today: date, weekday: int) -> date:
    days_ahead = (weekday - today.weekday()) % 7
    return today + timedelta(days=days_ahead or 7)


def _parse_day(text: str, today: date) -> date | None:
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    month_first = re.search(
        rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", text
    )
    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAMES})\.?(?:,?\s+(\d{{4}}))?\b", text
    )
    if month_first or day_first:
        if month_first:
            month = MONTHS[month_first.group(1)]
            day_number = int(month_first.group(2))
            year = month_first.group(3)
        else:
            assert day_first is not None
            day_number = int(day_first.group(1))
            month = MONTHS[day_first.group(2)]
            year = day_first.group(3)
        candidate = date(int(year) if year else today.year, month, day_number)
        if not year and candidate < today:
            candidate = date(today.year + 1, month, day_number)
        return candidate

    if re.search(r"\bday after tomorrow\b", text):
        return today + timedelta(days=2)
    if re.search(r"\b(tomorrow|tmrw|tmr)\b", text):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", text):
        return today
    weekday = re.search(rf"\b(?:next\s+|this\s+|on\s+)?({_WEEKDAY_NAMES})\b", text)
    if weekday:
        return _next_weekday(today, WEEKDAYS[weekday.group(1)])
    if re.search(r"\bnext week\b", text):
        return today + timedelta(days=7)
    return None


def parse_when(text: str, now: datetime | None = None) -> ParsedWhen:
    """Parse phrases like "tomorrow 9am", "Thu 16:00", "Nov 5 at 4", "in 25 min"."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    raw = (text or "").strip().lower()
    if not raw:
        raise ValueError("No time given.")
    raw = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})t(\d{1,2}:\d{2})", r"\1 \2", raw)

    relative = re.search(
        r"\bin\s+(an?|\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b", raw
    )
    if relative:
        amount_text = relative.group(1)
        amount = 1.0 if amount_text in ("a", "an") else float(amount_text)
        unit = relative.group(2)[0]
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        target = now + delta
        if unit in ("m", "h"):
            return ParsedWhen(target, has_time=True)
        clock, _ = _parse_clock(raw[: relative.start()] + " " + raw[relative.end():])
        return ParsedWhen(datetime.combine(target.date(), clock or DEFAULT_TIME), has_time=clock is not None)

    if raw == "now":
        return ParsedWhen(now, has_time=True)

    clock, rest = _parse_clock(raw)
    day = _parse_day(rest, now.date())

    if day is None and clock is None:
        raise ValueError(f'Could not understand the time "{text}".')

    if day is None:
        assert clock is not None
        candidate = datetime.combine(now.date(), clock)
        if candidate <= now:
            candidate += timedelta(days=1)
        return ParsedWhen(candidate, has_time=True)

    return ParsedWhen(datetime.combine(day, clock or DEFAULT_TIME), has_time=clock is not None)


def parse_day(text: str, now: datetime | None = None) -> date:
    """Parse a day reference ("today", "tomorrow", "Friday", "Nov 5")."""
    now = now or datetime.now()
    raw = (text or "today").strip().lower()
    day = _parse_day(raw, now.date())
    if day is None:
        raise ValueError(f'Could not understand the day "{text}".')
    return day


def describe(value: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    delta_days = (value.date() - now.date()).days
    clock = value.strftime("%-I:%M %p")
    if delta_days == 0:
        return f"today {clock}"
    if delta_days == 1:
        return f"tomorrow {clock}"
    if 1 < delta_days < 7:
        return f"{value.strftime('%A')} {clock}"
    return value.strftime("%a %-d %b %Y, ") + clock
