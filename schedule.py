"""Routine schedules: "every weekday at 8:30am", "every 2 hours", "daily at 9".

Parsed in plain code (like timeparse.py) so the model never does date arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from timeparse import WEEKDAYS

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAYPART_TIMES = {"morning": time(9, 0), "afternoon": time(14, 0), "evening": time(18, 0), "night": time(21, 0)}
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
                "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45}
MIN_INTERVAL = 5

_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?"
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"
_PATTERNS = [
    ("interval", rf"\bevery\s+((?:\d+|{'|'.join(sorted(NUMBER_WORDS, key=len, reverse=True))})\s+)?(minutes?|mins?|hours?|hrs?)\b"),
    ("weekdays", r"\b(?:every\s+(?:week\s?day|work\s?day)|on\s+weekdays|weekdays)\b"),
    # "every Monday" / "on Mondays" — but never a one-off "on Monday".
    ("weekly", rf"\b(?:every|each)\s+({_DAY}(?:\s*(?:,|and|&)\s*{_DAY})*)\b"),
    ("weekly", rf"\bon\s+({_DAY}s(?:\s*(?:,|and|&)\s*{_DAY}s)*)\b"),
    ("daily", r"\b(?:every\s?day|daily|each\s+day|every\s+(morning|afternoon|evening|night))\b"),
]


@dataclass(frozen=True)
class Schedule:
    kind: str
    at: str = "09:00"
    days: tuple[int, ...] = ()
    minutes: int = 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at, "days": list(self.days), "minutes": self.minutes}

    @classmethod
    def from_dict(cls, data: dict) -> "Schedule":
        return cls(data["kind"], data.get("at", "09:00"), tuple(data.get("days", ())), int(data.get("minutes", 0)))

    @property
    def clock(self) -> time:
        hour, minute = self.at.split(":")
        return time(int(hour), int(minute))

    def describe(self) -> str:
        clock = datetime.combine(datetime.today(), self.clock).strftime("%-I:%M %p")
        if self.kind == "interval":
            if self.minutes % 60 == 0:
                hours = self.minutes // 60
                return "Every hour" if hours == 1 else f"Every {hours} hours"
            return f"Every {self.minutes} minutes"
        if self.kind == "weekdays":
            return f"Every weekday at {clock}"
        if self.kind == "weekly":
            names = [DAY_NAMES[d] for d in self.days]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            return f"Every {joined} at {clock}"
        return f"Every day at {clock}"

    def next_after(self, moment: datetime) -> datetime:
        moment = moment.replace(second=0, microsecond=0)
        if self.kind == "interval":
            return moment + timedelta(minutes=self.minutes)
        allowed = {
            "daily": set(range(7)),
            "weekdays": set(range(5)),
            "weekly": set(self.days),
        }[self.kind]
        for offset in range(0, 8):
            candidate = datetime.combine(moment.date() + timedelta(days=offset), self.clock)
            if candidate > moment and candidate.weekday() in allowed:
                return candidate
        raise ValueError("schedule has no valid days")


def _clock(match: re.Match[str], daypart: str | None) -> time | None:
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    if hour > 23 or minute > 59 or (meridiem and not 1 <= hour <= 12):
        return None
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and hour < 12 and (daypart in {"afternoon", "evening", "night"} or 1 <= hour <= 6):
        hour += 12
    return time(hour, minute)


def _tidy(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"^\s*(?:,|and\b|then\b|to\b)\s*", "", text, flags=re.I)
    text = re.sub(r"\s*,\s*$", "", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text).strip(" ,")
    return text[:1].upper() + text[1:] if text else text


def _inside_quotes(text: str) -> bool:
    return text.count('"') % 2 == 1 or text.count("“") > text.count("”")


_LEAD_IN = r"\s*(?:(?:please|hey|ok|okay|so|and|human emulator|jarvis)[\s,]+)*"


def extract(goal: str) -> tuple[Schedule | None, str]:
    """Split a schedule phrase off a goal.

    "Every day at 9am, tell me my agenda" → (Every day at 9:00 AM, "Tell me my agenda").
    A phrase only counts when it is in schedule position — leading the goal, tied to a
    clock time, or (for intervals) ending it — so "write my daily standup notes" and
    "WhatsApp Sam I'll see you every morning" stay one-off missions."""
    for kind, pattern in _PATTERNS:
        for match in re.finditer(pattern, goal, flags=re.I):
            start, end = match.span()
            if _inside_quotes(goal[:start]):
                continue
            daypart = match.group(1).lower() if kind == "daily" and match.group(1) else None
            after = re.match(rf"\s*,?\s*(?:at\s+|@\s*)?{_TIME}(?!\s*(?:minutes?|mins?|hours?|hrs?|days?))", goal[end:], flags=re.I)
            before = re.search(rf"(?:\bat\s+)?\b{_TIME}\s*,?\s*$", goal[:start], flags=re.I)
            is_clock = lambda m: bool(m and (m.group(3) or m.group(2) or m.group(0).lower().lstrip(" ,").startswith(("at", "@"))))  # noqa: E731
            clock = None
            if kind != "interval" and is_clock(after):
                clock = _clock(after, daypart)
                if clock:
                    end += after.end()
            if clock is None and kind != "interval" and is_clock(before):
                clock = _clock(before, daypart)
                if clock:
                    start = before.start()
            leading = re.fullmatch(_LEAD_IN, goal[:start], flags=re.I) is not None
            trailing = not goal[end:].strip(" .!?")
            if not (leading or clock is not None or (kind == "interval" and trailing)):
                continue
            if kind == "interval":
                amount_text = (match.group(1) or "1").strip().lower()
                amount = int(amount_text) if amount_text.isdigit() else NUMBER_WORDS.get(amount_text, 1)
                minutes = amount * (60 if match.group(2).lower().startswith("h") else 1)
                schedule = Schedule("interval", minutes=max(MIN_INTERVAL, minutes))
            else:
                clock = clock or DAYPART_TIMES.get(daypart or "", time(9, 0))
                at = clock.strftime("%H:%M")
                if kind == "weekly":
                    found = re.findall(_DAY, match.group(1), flags=re.I)
                    schedule = Schedule("weekly", at=at, days=tuple(sorted({WEEKDAYS[d.lower()[:3]] for d in found})))
                else:
                    schedule = Schedule(kind, at=at)
            rest = _tidy(re.sub(rf"^{_LEAD_IN}", "", goal[:start], flags=re.I) + " " + goal[end:])
            return schedule, rest
    return None, goal
