"""Deterministic fast paths for requests a small on-device model gets wrong.

WhatsApp messages and small talk are parsed with rules instead of the 2B planner:
the contact and the exact words come from the user's sentence, so "say hi to Ridhi
on WhatsApp" can never turn into a message to the wrong chat or a made-up text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_WA = r"wh?ats\s?-?app|watsapp|whatsap"
WHATSAPP = re.compile(rf"\b(?:{_WA})\b", re.I)
_VERBS = r"say|send|text|message|msg|tell|ask|wish|ping|write|drop|greet|whatsapp"
# "hi, say hi to Ridhi…" — a greeting aimed at the assistant before the real request.
_LEAD_GREETING = re.compile(
    rf"^(?:hi+|hey+|hello|ok(?:ay)?|yo)\b[\s,!.]*(?:human\s+emulator\b[\s,!.]*)?(?=(?:\w+\s+){{0,3}}?(?:{_VERBS})\b)",
    re.I,
)
_POLITE = re.compile(
    r"^(?:(?:please|pls|plz|kindly|just|quickly|can you|could you|would you|will you|go ahead and|"
    r"i want you to|i need you to|i'd like you to|help me)\s+)+",
    re.I,
)
_WA_PHRASE = re.compile(
    rf"\s*\b(?:(?:on|via|over|through|using|in|by|with|from)\s+(?:my\s+|the\s+)?(?:{_WA})(?:\s+app)?|"
    rf"(?:a|an)\s+(?:{_WA})(?:\s+(?:message|msg|text))?)\b",
    re.I,
)
# A second task in the same sentence ("… and set volume to 20") belongs to the full planner.
_MORE_TASKS = re.compile(
    r"(?:\band\b|\bthen\b|\balso\b|[,;])\s+(?:then\s+|also\s+)?(?:set|create|make|remind|open|turn|switch|"
    r"read|block|schedule|add|check|play|search|go|put|close|launch|save|build|start|mute|change)\b",
    re.I,
)
_TRAILING = re.compile(r"(?:\s+(?:please|pls|now|right now|for me|asap|quickly|thanks|thank you))+$", re.I)
_QUOTED = re.compile(r"[\"“”](.+?)[\"“”]|(?<!\w)'(.+?)'(?!\w)")
_COMPOSE_KINDS = (
    r"letter|message|msg|note|poem|apology|wish(?:es)?|text|paragraph|story|joke|quote|shayari|compliment|"
    r"greeting|invitation|invite|thank[- ]you(?: note| message)?|reminder"
)
_FESTIVALS = {
    "diwali": "🪔", "deepavali": "🪔", "holi": "🎨", "eid": "🌙", "christmas": "🎄", "xmas": "🎄",
    "navratri": "✨", "dussehra": "✨", "pongal": "✨", "onam": "✨", "lohri": "🔥", "easter": "🐣",
    "new year": "🎉", "birthday": "🎂", "anniversary": "💐", "ganesh chaturthi": "🙏", "raksha bandhan": "🎀",
}
_PROPER = {"diwali", "deepavali", "holi", "eid", "christmas", "xmas", "navratri", "dussehra", "pongal",
           "onam", "lohri", "easter", "new", "year", "ganesh", "chaturthi", "raksha", "bandhan", "mubarak"}


@dataclass
class WhatsAppIntent:
    contact: str
    message: str = ""
    compose: str = ""  # instruction for the on-device model when the user asked for a written piece


def _strip_lead(text: str) -> str:
    text = text.strip().strip("\"'“”")
    text = _LEAD_GREETING.sub("", text, count=1)
    return _POLITE.sub("", text.strip(" ,")).strip()


def _clean_contact(raw: str) -> str:
    name = _TRAILING.sub("", raw.strip(" .,!?:;-\"'“”"))
    name = re.sub(r"^(?:my|the|our)\s+", "", name, flags=re.I).strip(" .,!?:;-\"'“”")
    if not name or len(name) > 40 or len(name.split()) > 4 or not re.search(r"\w", name):
        return ""
    return name.title() if name == name.lower() else name


def _clean_message(raw: str) -> str:
    text = _TRAILING.sub("", " ".join(raw.split()).strip(" ,:;-\"'“”"))
    text = re.sub(r"\bi\b", "I", text)
    text = re.sub(r"\bi(['’](?:m|ll|ve|d))\b", r"I\1", text, flags=re.I)
    return text[:1].upper() + text[1:] if text else ""


def _split_name(rest: str, chats: list[str]) -> tuple[str, str]:
    """"ridhi sharma I'm late" → the longest known chat name at the start, else the first word."""
    words = rest.split()
    known = {c.lower() for c in chats}
    for n in range(min(4, len(words) - 1), 1, -1):
        if " ".join(words[:n]).lower().strip(",:") in known:
            return " ".join(words[:n]), " ".join(words[n:])
    return words[0], " ".join(words[1:])


def _wish_message(occasion: str) -> str:
    lowered = occasion.lower().strip(" .!")
    if re.match(r"good\s+(morning|night|evening|afternoon)", lowered):
        part = lowered.split()[1]
        return f"Good {part}! " + {"morning": "☀️", "night": "🌙"}.get(part, "😊")
    if re.match(r"congrat", lowered):
        return "Congratulations! 🎉"
    if re.match(r"(good\s+)?luck|all the best|best of luck", lowered):
        return "All the best! 👍"
    words = [w.capitalize() if w in _PROPER else w for w in lowered.split()]
    phrase = " ".join(words)
    if not re.match(r"(a|an)\s", phrase):
        phrase = f"a {phrase}"
    emoji = next((e for key, e in _FESTIVALS.items() if key in lowered), "✨")
    return f"Wishing you {phrase}! {emoji}"


def parse_whatsapp(goal: str, chats: list[str] | None = None) -> WhatsAppIntent | None:
    """Return who to message and what to send, or None if this is not a clear WhatsApp request."""
    if not WHATSAPP.search(goal):
        return None
    chats = chats or []
    text = _strip_lead(goal)
    text = re.sub(rf"^(?:{_WA})\s+", "message ", text, flags=re.I)
    text = _WA_PHRASE.sub("", text)
    text = re.sub(r"^(?:(?:text|send|write|message)\s+and\s+)+", "", text, flags=re.I)
    text = " ".join(text.split()).strip(" .!?,")
    if not text or _MORE_TASKS.search(text):
        return None

    quoted = _QUOTED.search(text)
    if quoted:
        message = quoted.group(1) or quoted.group(2)
        rest = (text[: quoted.start()] + " " + text[quoted.end():]).strip()
        to = re.search(r"\b(?:to|for)\s+(.+)$", rest, re.I)
        if to:
            contact = _clean_contact(to.group(1))
        else:
            after_verb = re.sub(rf"^(?:{_VERBS})\s+", "", rest, flags=re.I)
            contact = _clean_contact(after_verb.split(" saying")[0]) if after_verb else ""
        return WhatsAppIntent(contact, " ".join(message.split())) if contact else None

    m = re.match(
        r"^wish\s+(?P<c>.+?)\s+(?P<o>(?:(?:a|an)\s+)?(?:(?:very|really|super)\s+)?(?:happy|merry|good|warm|blessed)\b.*"
        r"|congrat\w*.*|(?:good\s+)?luck\b.*|all the best\b.*|best of luck\b.*)$",
        text, re.I,
    )
    if m:
        contact = _clean_contact(m.group("c"))
        return WhatsAppIntent(contact, _wish_message(m.group("o"))) if contact else None

    kinds = rf"(?P<k>(?:[\w'’-]+\s+){{0,4}}?(?:{_COMPOSE_KINDS}))"
    topic = r"(?:\s+(?:about|for|on|regarding|saying|that)\s+(?P<t>.+))?"
    for pattern in (
        rf"^(?:send|text|write|message|drop)\s+(?P<c>.+?)\s+(?:a|an|some|the|my)\s+{kinds}{topic}$",
        rf"^(?:send|write|text|drop)\s+(?:a|an|some|the|my)\s+{kinds}\s+(?:to|for)\s+(?P<c>.+?){topic}$",
    ):
        m = re.match(pattern, text, re.I)
        if m:
            contact = _clean_contact(m.group("c"))
            if not contact:
                return None
            kind = m.group("k").strip().lower()
            article = "an" if kind[:1] in "aeiou" else "a"
            about = f" about {m.group('t').strip()}" if m.group("t") else ""
            instruction = (
                f"Write {article} {kind} to {contact}{about}, to send on WhatsApp. "
                f"Write it as me, speaking directly to {contact}. Warm, sincere and natural, 2-4 short sentences, "
                "under 70 words. Do not invent events, names or details I did not mention, and never blame anyone; "
                "an apology takes responsibility. Output only the message: no subject line, no quotes, "
                "no placeholders like [Name], no signature."
            )
            return WhatsAppIntent(contact, compose=instruction)

    m = re.match(r"^(?P<v>tell|ask)\s+(?P<rest>.+?)\s+to\s+(?P<m>.+)$", text, re.I)
    if m and len(m.group("rest").split()) <= 3:
        contact = _clean_contact(m.group("rest"))
        ask = m.group("v").lower() == "ask"
        body = _clean_message(m.group("m"))
        message = f"Can you {body[:1].lower() + body[1:]}?" if ask else f"Please {body[:1].lower() + body[1:]}."
        return WhatsAppIntent(contact, message) if contact and body else None

    m = re.match(r"^(?:say|send|text|write|message|msg|drop|ping)\s+(?P<m>.+?)\s+to\s+(?P<c>.+)$", text, re.I)
    if m:
        # Garbled voice ("send hi to Hi … to ridhi"): the recipient is after the last "to".
        contact = _clean_contact(re.split(r"\s+to\s+", m.group("c"), flags=re.I)[-1])
        message = _clean_message(m.group("m"))
        return WhatsAppIntent(contact, message) if contact and message else None

    m = re.match(
        r"^(?:text|message|msg|ping|send|tell)\s+(?P<c>.+?)\s+(?:saying|that says|to say|that|with|:|-)\s+(?P<m>.+)$",
        text, re.I,
    )
    if m and len(m.group("c").split()) <= 3:
        contact = _clean_contact(m.group("c"))
        message = _clean_message(m.group("m"))
        return WhatsAppIntent(contact, message) if contact and message else None

    m = re.match(r"^(?:text|message|msg|ping|tell)\s+(?P<rest>.+)$", text, re.I)
    if m and len(m.group("rest").split()) >= 2:
        name, body = _split_name(m.group("rest"), chats)
        contact, message = _clean_contact(name), _clean_message(body)
        return WhatsAppIntent(contact, message) if contact and message else None

    m = re.match(r"^greet\s+(?P<c>.+)$", text, re.I)
    if m:
        contact = _clean_contact(m.group("c"))
        return WhatsAppIntent(contact, "Hi! 👋") if contact else None

    # "say ridhi good night" — the name comes first. Only trusted when it is a real chat name,
    # or (WhatsApp not readable) when the first word is not an everyday message word.
    m = re.match(r"^(?:say|send)\s+(?P<rest>.+)$", text, re.I)
    if m and len(m.group("rest").split()) >= 2:
        name, body = _split_name(m.group("rest"), chats)
        known = {c.lower() for c in chats}
        if name.lower() in known or (not chats and name.lower() not in _MESSAGE_WORDS):
            contact, message = _clean_contact(name), _clean_message(body)
            return WhatsAppIntent(contact, message) if contact and message else None
    return None


_MESSAGE_WORDS = {
    "hi", "hii", "hello", "hey", "good", "thanks", "thank", "sorry", "i", "i'm", "im", "ok", "okay", "bye",
    "love", "congrats", "congratulations", "happy", "welcome", "yes", "no", "please", "what", "how", "where",
    "when", "why", "see", "miss", "call", "come", "let's", "lets", "we", "you", "u", "it", "this", "that",
    "the", "a", "an", "my", "all", "best", "great", "nice", "sweet", "take", "get", "have", "be", "on", "in",
}


def missing_recipient(goal: str) -> bool:
    """"Say hi on WhatsApp" — a message with nobody to send it to."""
    if not WHATSAPP.search(goal):
        return False
    text = _WA_PHRASE.sub("", _strip_lead(goal)).strip(" .!?")
    return bool(re.fullmatch(r"(?:say|send)\s+[\w\s'’!,]{1,40}", text, re.I)) and " to " not in f" {text.lower()} "


# ---------------------------------------------------------------------------
# Small talk
# ---------------------------------------------------------------------------

_HELLO = re.compile(
    r"^(?:hi+|hello+|hey+|heya|hiya|hola|namaste|yo|sup|good\s+(?:morning|afternoon|evening))"
    r"(?:\s+(?:there|human\s+emulator|emulator|buddy|bro|man|friend))*[\s!.,?]*$",
    re.I,
)
_HOW = re.compile(r"^(?:(?:hi|hey|hello)[\s,!]+)?(?:how\s+(?:are|r)\s+(?:you|u)|how're you|how do you do|how is it going|how's it going|what'?s up)\b.*$", re.I)
_WHO = re.compile(r"^(?:who|what)\s+(?:are|r)\s+(?:you|u)\b.*$|^what can you do\b.*$|^help\??$|^what do you do\b.*$", re.I)
_THANKS = re.compile(r"^(?:thanks|thank\s+you|thank\s+u|thx|ty|great job|good job|well done|nice work|awesome|perfect|cool)\b[\s\w!.]*$", re.I)

CAPABILITIES = (
    "I can message people on WhatsApp, create reminders, calendar events and notes, write files and "
    "Pages documents, open apps and projects in VS Code, read your screen, read your agenda aloud and "
    "check your Mac's health — and I check every change by reading it back."
)


def _part_of_day(now: datetime) -> str:
    return "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"


def small_talk(goal: str, now: datetime) -> str | None:
    """A friendly reply for greetings and chit-chat, so they never turn into actions."""
    text = " ".join(goal.split()).strip()
    if not text or len(text) > 80:
        return None
    if _HOW.match(text):
        return "I'm running smoothly, fully on this Mac and ready to help. What should I do for you?"
    if _HELLO.match(text):
        return (
            f"Good {_part_of_day(now)}! I'm Human Emulator, running entirely on this Mac. "
            "Tell me what to do — for example “say hi to Sam on WhatsApp”."
        )
    if _WHO.match(text):
        return f"I'm Human Emulator, an on-device assistant for this Mac. {CAPABILITIES}"
    if _THANKS.match(text):
        return "You're welcome! Anything else?"
    return None


def is_noise(goal: str) -> bool:
    """Punctuation or a stray key press, not a request."""
    return not re.search(r"[^\W_]", goal)


# ---------------------------------------------------------------------------
# Calculator, "press X in Y" and "describe my screen"
# ---------------------------------------------------------------------------

_CALC_CUE = re.compile(r"\b(calculator|calc|calculate|compute|work out|solve|evaluate|what(?:'s|s| is))\b", re.I)
_BETWEEN = r"(?<=\d)\s*{}\s*(?=[\d(-])"
_CALC_WORDS = [
    (_BETWEEN.format(r"(?:multiplied\s+by|times|into|[x×✕*])"), "*"),
    (_BETWEEN.format(r"(?:divided\s+by|over|[÷/])"), "/"),
    (_BETWEEN.format(r"(?:plus|\+)"), "+"),
    (_BETWEEN.format(r"(?:minus|[-−–])"), "-"),
]
_EXPRESSION = re.compile(r"-?\d+(?:\.\d+)?(?:[-+*/]\d+(?:\.\d+)?)+")


def parse_calculation(goal: str) -> str | None:
    """"Use Calculator to work out 128 × 46" → "128*46" (numbers joined by + - * /)."""
    if not _CALC_CUE.search(goal):
        return None
    text = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", goal.lower())
    for pattern, symbol in _CALC_WORDS:
        text = re.sub(pattern, symbol, text)
    match = _EXPRESSION.search(text.replace(" ", ""))
    if not match or len(match.group(0)) > 40:
        return None
    return match.group(0)


def evaluate(expression: str) -> float:
    """Exact arithmetic with normal precedence (as Calculator does); only numbers and + - * /."""
    import ast
    import operator

    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](walk(node.left), walk(node.right))
        raise ValueError("only numbers and + - × ÷ are allowed")

    return walk(ast.parse(expression, mode="eval").body)


def pretty_expression(expression: str) -> str:
    spaced = re.sub(r"(?<=[\d)])([-+*/])", r" \1 ", expression)
    return spaced.replace("*", "×").replace("/", "÷").replace(" - ", " − ")


_PRESS = re.compile(
    r"^(?:(?:please|pls|can you|could you|just)\s+)*(?:press|click|tap|hit|push)\s+(?:on\s+)?(?:the\s+)?"
    r"[\"“']?(?P<button>[^\"”']+?)[\"”']?(?:\s+button)?\s+(?:in|on|inside)\s+(?:the\s+)?"
    r"(?P<app>[\w .&+-]+?)(?:\s+app)?[\s.!?]*$",
    re.I,
)


def parse_press(goal: str) -> tuple[str, str] | None:
    """"Press Show Sidebar in Calculator" → ("Show Sidebar", "Calculator")."""
    match = _PRESS.match(" ".join(goal.split()))
    if not match:
        return None
    button, app = match.group("button").strip(), match.group("app").strip()
    return (button, app) if button and app and len(button) <= 60 else None


_DESCRIBE = re.compile(
    r"^(?:(?:please|pls|can you|could you|hey|ok|okay)[\s,]+)*(?:"
    r"(?:describe|explain)\s+(?:what(?:'s|s| is)\s+on\s+)?(?:my|the|this)\s+(?:(?P<app>[\w .&+-]+?)\s+)?(?:screen|window|display)"
    r"|(?:tell me\s+)?what(?:'s|s| is)\s+on\s+(?:my|the|this)\s+(?:screen|window)"
    r"|read\s+(?:my|the|this)\s+(?:screen|window)\s+(?:aloud|out loud|to me)"
    r")(?:\s+(?:to me|aloud|out loud|for me))?[\s.!?]*$",
    re.I,
)


def parse_describe(goal: str) -> str | None:
    """"Describe my screen" → "" (the window behind the dashboard); "describe my WhatsApp screen" → "WhatsApp"."""
    match = _DESCRIBE.match(" ".join(goal.split()))
    if not match:
        return None
    return (match.group("app") or "").strip()


_PAGES_DOC = re.compile(
    r"^(?:(?:please|pls|can you|could you|just)\s+)*(?:write|create|draft|make|prepare)\s+(?:me\s+)?(?:a|an)\s+"
    r"(?P<kind>(?:[\w-]+\s+){0,3}?(?:essay|report|letter|article|summary|story|speech|poem|document|write-?up|note))\s+"
    r"(?:(?:to|for)\s+(?P<to>(?:the\s+|my\s+|our\s+)?[\w.'-]+(?:\s+[\w.'-]+)?)\s+)?"
    r"(?:about|on|regarding|explaining|describing|for)\s+(?P<topic>.+?)\s+(?:in|using|with|on)\s+(?:apple\s+)?pages"
    r"(?:\s+app)?(?P<rest>.*)$",
    re.I,
)
_SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "for", "why", "how", "with", "about"}


@dataclass
class PagesRequest:
    kind: str
    topic: str
    title: str
    pdf: bool


def parse_pages(goal: str) -> PagesRequest | None:
    """"Write a one-page essay about why on-device AI agents matter in Pages and export it as a PDF"."""
    match = _PAGES_DOC.match(" ".join(goal.split()).rstrip(" .!"))
    if not match or _MORE_TASKS.search(match.group("rest")):
        return None
    topic = match.group("topic").strip(" .,")
    words = topic.split()
    title = " ".join(w if any(c.isupper() for c in w[1:]) else (w.lower() if i and w.lower() in _SMALL_WORDS else w[:1].upper() + w[1:])
                     for i, w in enumerate(words))
    kind = match.group("kind").strip().lower() + (f" to {match.group('to').strip()}" if match.group("to") else "")
    return PagesRequest(kind, topic, title[:80], bool(re.search(r"\bpdfs?\b", match.group("rest"), re.I)))


# ---------------------------------------------------------------------------
# Reminders and calendar blocks named in the goal (the model must not change their times)
# ---------------------------------------------------------------------------

_WHEN_START = (
    r"(?:in\s+(?:\d+|an?|half an)\s*(?:min(?:ute)?s?|hours?|hrs?)|at\s+\d|on\s+\w|tomorrow|today|tonight|"
    r"this\s+\w+|next\s+\w+|(?:mon|tues|wednes|thurs|fri|satur|sun)day)"
)
_CLAUSE_END = r"(?=\s*(?:[,;]|\.(?:\s|$)|$|\band\b|\bthen\b))"
_REMIND = re.compile(
    rf"\bremind me\s+(?:(?P<w1>{_WHEN_START}[^,;]*?)\s+to\s+(?P<t1>[^,;]+?)"
    rf"|to\s+(?P<t2>[^,;]+?)\s+(?P<w2>{_WHEN_START}[^,;]*?)){_CLAUSE_END}",
    re.I,
)
_BLOCK = re.compile(
    rf"\bblock\s+(?:out\s+)?(?P<n>\d+)\s*(?P<unit>min(?:ute)?s?|hours?|hrs?)\s+(?P<when>{_WHEN_START}[^,;]*?)"
    rf"\s+for\s+(?P<title>[^,;]+?){_CLAUSE_END}",
    re.I,
)


@dataclass
class ScheduleClause:
    kind: str  # "reminder" | "event"
    when: str
    title: str
    minutes: int = 0


def _title(text: str) -> str:
    text = re.sub(r"^(?:a|an|the|my)\s+", "", text.strip(" .,"), flags=re.I)
    return text[:1].upper() + text[1:]


def schedule_clauses(goal: str) -> list[ScheduleClause]:
    """"…remind me tomorrow at 9am to rehearse, and block 45 minutes on Thursday at 4pm for a mock viva"."""
    found: list[tuple[int, ScheduleClause]] = []
    for m in _REMIND.finditer(goal):
        when = (m.group("w1") or m.group("w2")).strip()
        found.append((m.start(), ScheduleClause("reminder", when, _title(m.group("t1") or m.group("t2")))))
    for m in _BLOCK.finditer(goal):
        minutes = int(m.group("n")) * (60 if m.group("unit").lower().startswith(("hour", "hr")) else 1)
        when = re.sub(r"^on\s+", "", m.group("when").strip(), flags=re.I)
        found.append((m.start(), ScheduleClause("event", when, _title(m.group("title")), minutes)))
    return [clause for _start, clause in sorted(found, key=lambda pair: pair[0])]
