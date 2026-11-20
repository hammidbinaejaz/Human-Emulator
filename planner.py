"""Goal → validated plan, using the on-device model with constrained JSON output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import intents
import llm
import timeparse
from actions import (
    CATALOG,
    COMPOSE_SYSTEM,
    ArgError,
    PlanContext,
    locate_user_path,
    pick_chat,
    pretty_path,
    whatsapp_visible_chats,
)

MAX_STEPS = 8
# Actions the model may choose; fast-path-only actions (like "reply") stay off its menu.
PLANNER_CATALOG = {name: action for name, action in CATALOG.items() if action.planner_visible}

# One schema variant per action: the model picks an action and is then forced to
# write exactly that action's arguments, in order, with required ones present.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "unsupported": {"type": "string"},
        "steps": {"type": "array", "items": {"anyOf": [a.json_schema() for a in PLANNER_CATALOG.values()]}},
    },
    "required": ["title", "unsupported", "steps"],
}

# Safety net: if the goal asks for something no audited action can do and the model
# forgot to say so, the agent says so itself instead of silently ignoring it.
# (pattern in the goal, category, words that show the model already explained it)
OUT_OF_SCOPE: list[tuple[str, str, tuple[str, ...]]] = [
    (r"\b(imessage|sms|telegram|slack|discord|dm)\b", "Sending messages", ("messag", "text", "chat")),
    (r"\b(e-?mail|gmail|outlook)\b", "Sending email", ("mail",)),
    (r"\b(tweet|instagram|linkedin|facebook)\b", "Posting to social media", ("post", "social")),
    (r"\b(buy|order|purchase|pay|checkout)\b", "Purchases and orders", ("order", "purchas", "buy", "pay")),
    (r"\b(delete|erase|wipe|uninstall)\b", "Deleting things", ("delet", "eras", "remov")),
    (r"\b(weather|news|stock price|search the web|google it|browse)\b", "Live web lookups", ("web", "internet", "online", "search")),
]

RULES = """Rules:
- First write "unsupported": one short sentence naming any part of the goal these actions cannot do (email, SMS/iMessage, web search, payments, deleting things). Use "" if everything is possible.
- Then the steps: the fewest that fully achieve the rest of the goal (at most 8).
- Paths live in Desktop, Documents or Downloads, e.g. "Desktop/Viva Prep/checklist.md".
- For "when" and "day", copy the user's own wording, e.g. "tomorrow 9am", "Thursday 4pm", "in 25 minutes". Never calculate dates. Use "" for a reminder with no time.
- "{{stepN}}" inserts the text produced by step N (read_screen, compose, read_agenda, mac_health, write_file, pages_read).
- Write real, specific content. Never write placeholders like "content here" or "[Name]".
- If the user wants something said, read out or told aloud, end with a speak step.
- build_webpage writes its own content: put every fact the user gave into "details" instead of adding a compose step.
- Long writing for Pages (essays, letters, reports): compose it first, then pages_create with content "{{stepN}}".
- To open a folder or file in VS Code use open_in_vscode, never open_app.
- send_whatsapp only when the user asks for WhatsApp: copy the contact name and the message exactly as the user wrote them.
- "title" names the mission in 2-4 words."""

EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    (
        "Create a Launch folder on my desktop with a todo list, remind me Friday at 10am to review it, and order a pizza",
        {
            "title": "Launch prep",
            "unsupported": "Ordering food is outside Human Emulator's audited actions.",
            "steps": [
                {"action": "create_folder", "args": {"path": "Desktop/Launch"}},
                {
                    "action": "write_file",
                    "args": {
                        "path": "Desktop/Launch/todo.md",
                        "content": "# Launch todo\n- [ ] Finalise the slides\n- [ ] Rehearse the live demo\n- [ ] Share the release notes",
                    },
                },
                {"action": "create_reminder", "args": {"title": "Review the launch todo", "when": "Friday 10am"}},
            ],
        },
    ),
    (
        "Read what's on my screen and save the key points as a note",
        {
            "title": "Screen to note",
            "unsupported": "",
            "steps": [
                {"action": "read_screen", "args": {}},
                {
                    "action": "compose",
                    "args": {"instruction": "Summarise the key points as 3-5 short bullet points.", "input": "{{step1}}"},
                },
                {"action": "create_note", "args": {"title": "Screen summary", "content": "{{step2}}"}},
            ],
        },
    ),
    (
        "Deep work mode: go dark, volume 10, block 30 minutes tomorrow at 2pm for writing and WhatsApp Sam that I'm busy",
        {
            "title": "Deep work mode",
            "unsupported": "",
            "steps": [
                {"action": "set_dark_mode", "args": {"enabled": True}},
                {"action": "set_volume", "args": {"level": 10}},
                {"action": "create_event", "args": {"title": "Deep work: writing", "when": "tomorrow 2pm", "duration_minutes": 30}},
                {"action": "send_whatsapp", "args": {"contact": "Sam", "message": "I'm busy right now, will get back to you soon."}},
            ],
        },
    ),
    (
        "What's on my calendar tomorrow? Tell me out loud",
        {
            "title": "Tomorrow briefing",
            "unsupported": "",
            "steps": [
                {"action": "read_agenda", "args": {"day": "tomorrow"}},
                {"action": "speak", "args": {"text": "{{step1}}"}},
            ],
        },
    ),
]


def system_prompt() -> str:
    catalog = "\n".join(
        f"{action.name} {action.signature} - {action.summary}" for action in PLANNER_CATALOG.values()
    )
    return (
        "You are the mission planner inside Human Emulator, an offline macOS agent running on the user's Mac.\n"
        "Turn the user's goal into a short ordered plan. You may ONLY use these audited actions:\n\n"
        f"{catalog}\n\n{RULES}"
    )


def base_messages(goal: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": system_prompt()}]
    for example_goal, example_plan in EXAMPLES:
        messages.append({"role": "user", "content": example_goal})
        messages.append({"role": "assistant", "content": json.dumps(example_plan)})
    messages.append({"role": "user", "content": goal})
    return messages


@dataclass
class PlannedStep:
    action: str
    args: dict[str, Any]
    label: str
    risk: str
    app: str | None
    produces_text: bool


@dataclass
class Plan:
    title: str
    steps: list[PlannedStep]
    unsupported: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # How the plan was made, shown in the mission log (not as warnings).
    notes: list[str] = field(default_factory=list)


_TEMPLATE = re.compile(r"\{\{?\s*(?:step\s*)?(\d+)(?:\s*\.\s*\w+)?\s*\}?\}", re.I)
_PLACEHOLDER = re.compile(
    r"\[(?:[^\]]{0,30}\b(?:name|insert|your|details?|project|topic|date|placeholder)\b[^\]]{0,30})\]"
    r"|lorem ipsum|content here|text here|<insert",
    re.I,
)
_SENDS = re.compile(r"\b(send|post|order|buy|pay|book|share with|email|whatsapp|text)\b", re.I)
_DRAFTING = re.compile(r"\b(draft|write|compose|prepare)\b", re.I)
# The small model likes to narrate with an extra speak step; keep it only when speech was asked for.
_SPOKEN = re.compile(r"\b(say|said|tell|speak|aloud|out loud|read (?:it|them|that|this|out|me)|announce|voice|talk|hear)\b", re.I)
# Fields where a {{stepN}} reference is never meaningful.
_SCALAR_FIELDS = {"when", "day", "level", "enabled", "duration_minutes", "delay_seconds", "path", "app", "contact", "export_pdf"}


def canonical_templates(value: Any) -> Any:
    if isinstance(value, str):
        return _TEMPLATE.sub(lambda m: "{{step" + m.group(1) + "}}", value)
    return value


def referenced_steps(value: Any) -> list[int]:
    if not isinstance(value, str):
        return []
    return [int(n) for n in re.findall(r"\{\{step(\d+)\}\}", value)]


def _fold_compose_into_webpage(raw_steps: list[Any]) -> tuple[list[Any], int]:
    """compose(facts) → build_webpage(details={{stepK}}) costs two slow generations.
    build_webpage writes its own copy, so hand it the facts directly and skip compose."""
    steps = [dict(s) if isinstance(s, dict) else s for s in raw_steps]
    text_of = lambda s: json.dumps(s.get("args", {})) if isinstance(s, dict) else ""  # noqa: E731
    refs: dict[int, int] = {}
    for step in steps:
        for n in referenced_steps(canonical_templates(text_of(step))):
            refs[n] = refs.get(n, 0) + 1
    folded = 0
    for step in steps:
        if not isinstance(step, dict) or step.get("action") != "build_webpage":
            continue
        args = dict(step.get("args") or {})
        match = re.fullmatch(r"\{\{step(\d+)\}\}", canonical_templates(str(args.get("details", ""))).strip())
        if not match:
            continue
        k = int(match.group(1))
        source = steps[k - 1] if 1 <= k <= len(steps) else None
        if not isinstance(source, dict) or source.get("action") != "compose" or refs.get(k) != 1:
            continue
        compose_args = source.get("args") or {}
        facts = str(compose_args.get("input") or compose_args.get("instruction") or "")
        if referenced_steps(canonical_templates(facts)):
            continue
        args["details"] = facts
        step["args"] = args
        steps[k - 1] = None
        folded += 1
    return steps, folded


def _fold_vscode(raw_steps: list[Any], goal: str) -> tuple[list[Any], bool]:
    """open_path(X) + open_app(VS Code) opens X in Finder and an empty editor.
    What the user meant is open_in_vscode(X)."""
    if not re.search(r"\b(vs ?code|visual studio code)\b", goal, re.I):
        return raw_steps, False
    steps = [dict(s) if isinstance(s, dict) else s for s in raw_steps]
    is_vscode = lambda s: isinstance(s, dict) and s.get("action") == "open_app" and re.search(  # noqa: E731
        r"\b(vs ?code|visual studio code|code)\b", json.dumps(s.get("args", {})), re.I)
    opener = next((i for i, s in enumerate(steps) if is_vscode(s)), None)
    target = next((i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("action") == "open_path"), None)
    if opener is None or target is None:
        return raw_steps, False
    args = steps[target].get("args") or {}
    path = next((v for k, v in args.items() if k in {"target", "path", "folder", "file"} and isinstance(v, str)), "")
    if not path or re.match(r"^https?://", path):
        return raw_steps, False
    steps[target] = {"action": "open_in_vscode", "args": {"path": path}}
    steps[opener] = None
    return steps, True


def _whatsapp_requested(goal: str, contact: str) -> str | None:
    """One-way messages need an explicit request: WhatsApp named and the contact in the goal."""
    if not re.search(r"whats\s?app", goal, re.I):
        return "the goal never asked for a WhatsApp message"
    squash = lambda s: re.sub(r"\W+", " ", s).strip().lower()  # noqa: E731
    if not contact or f" {squash(contact)} " not in f" {squash(goal)} ":
        return f"the contact “{contact}” is not named in the goal"
    return None


_OPEN_TARGETS = [
    re.compile(r"\bopen\s+(?:up\s+)?(?:my\s+|the\s+|our\s+)?(.+?)\s*(?:folder|project|directory|repo|repository|file)?\s+in\s+(?:vs ?code|visual studio code|code)\b", re.I),
    re.compile(r"\bopen\s+(?:up\s+)?(?:my\s+|the\s+|our\s+)?(.+?)\s+(?:folder|directory|file)\b", re.I),
]


def ground_path(goal: str, known_folders: list) -> str | None:
    """The folder the user actually named, if it exists (small models invent names)."""
    for pattern in _OPEN_TARGETS:
        match = pattern.search(goal)
        if not match:
            continue
        try:
            found = locate_user_path(match.group(1).strip(" .'\"“”"), known_folders)
        except ArgError:
            continue
        if found.exists():
            return str(found)
    return None


def _ground_schedule(raw_steps: list[Any], goal: str, now: datetime) -> tuple[list[Any], list[str]]:
    """Reminders/events the user named keep the user's own time words, and none are silently dropped."""
    clauses = intents.schedule_clauses(goal)
    if not clauses:
        return raw_steps, []
    notes: list[str] = []
    steps = [dict(s) if isinstance(s, dict) else s for s in raw_steps]

    def same_time(a: str, b: str) -> bool:
        try:
            return timeparse.parse_when(a, now).value == timeparse.parse_when(b, now).value
        except ValueError:
            return False

    for kind, name in (("reminder", "create_reminder"), ("event", "create_event")):
        wanted = [c for c in clauses if c.kind == kind]
        planned = [s for s in steps if isinstance(s, dict) and s.get("action") == name]
        for step, clause in zip(planned, wanted):
            args = CATALOG[name].canonical_args(step.get("args") if isinstance(step.get("args"), dict) else {})
            said = str(args.get("when") or "")
            if not same_time(said, clause.when):
                notes.append(f"Grounded: the model wrote “{said or 'no time'}”; using “{clause.when}” from your request.")
                args["when"] = clause.when
            if kind == "event":
                args["duration_minutes"] = clause.minutes
            step["args"] = args
        for clause in wanted[len(planned):]:
            args: dict[str, Any] = {"title": clause.title, "when": clause.when}
            if kind == "event":
                args["duration_minutes"] = clause.minutes
            steps.append({"action": name, "args": args})
            notes.append(f"Added: you asked for {'an' if kind == 'event' else 'a'} {kind} “{clause.title}” ({clause.when}) that the model's plan missed.")
    return steps, notes


_CONTEXT_NOTE = re.compile(r"\s*\(?Context:\s*it is[^)\n]*\)?\.?", re.I)
_PAGES_PHRASE = re.compile(r"\s+(?:in|for|into|on|using)\s+(?:apple\s+)?pages(?:\s+(?:app|document))?\b", re.I)


def _clean_raw_args(raw_steps: list[Any]) -> list[Any]:
    """Remove the date hint the model sometimes copies into text, and "in Pages" from essay topics."""
    has_pages = any(isinstance(s, dict) and s.get("action") == "pages_create" for s in raw_steps)
    cleaned = []
    for step in raw_steps:
        if isinstance(step, dict) and isinstance(step.get("args"), dict):
            args = {k: _CONTEXT_NOTE.sub("", v).strip() if isinstance(v, str) else v for k, v in step["args"].items()}
            if has_pages and step.get("action") == "compose":
                args = {k: _PAGES_PHRASE.sub("", v) if isinstance(v, str) else v for k, v in args.items()}
            step = {**step, "args": args}
        cleaned.append(step)
    return cleaned


def _example_strings() -> set[str]:
    found: set[str] = set()
    for _goal, plan in EXAMPLES:
        for step in plan["steps"]:
            for key, value in step["args"].items():
                if key in {"title", "path", "content", "message", "instruction", "details"} and isinstance(value, str) \
                        and len(value) >= 8 and not value.startswith("{{"):
                    found.add(value.strip().lower())
    return found


_EXAMPLE_STRINGS = _example_strings()
_EXAMPLE_STEPS = [json.dumps(s, sort_keys=True) for _g, p in EXAMPLES for s in p["steps"]]
_TOPIC_WORDS = {"set_dark_mode": ("dark", "light", "night"), "set_volume": ("volume", "sound", "loud", "quiet", "mute")}


def _copied_from_examples(raw_steps: list[Any], goal: str) -> set[int]:
    """Positions of steps the model copied from the few-shot examples instead of planning the goal."""
    words = set(re.findall(r"[a-z0-9]+", goal.lower()))
    leaked: set[int] = set()
    for position, step in enumerate(raw_steps, start=1):
        if not isinstance(step, dict):
            continue
        for value in (step.get("args") or {}).values() if isinstance(step.get("args"), dict) else []:
            text = value.strip().lower() if isinstance(value, str) else ""
            if text in _EXAMPLE_STRINGS and not set(re.findall(r"[a-z]{3,}", text)) <= words:
                leaked.add(position)
    if leaked:
        for position, step in enumerate(raw_steps, start=1):
            action = str(step.get("action")) if isinstance(step, dict) else ""
            if action in _TOPIC_WORDS and json.dumps(step, sort_keys=True) in _EXAMPLE_STEPS:
                if not any(w in words for w in _TOPIC_WORDS[action]):
                    leaked.add(position)
    return leaked


def validate(raw: dict[str, Any], now: datetime, goal: str = "") -> Plan:
    """Normalise model output into executable steps; collect problems instead of guessing."""
    ctx = PlanContext(now=now)
    steps: list[PlannedStep] = []
    errors: list[str] = []
    warnings: list[str] = []
    # Model step number → position in the validated plan (steps can be rejected).
    index_map: dict[int, int] = {}
    raw_steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    if len(raw_steps) > MAX_STEPS:
        errors.append(f"The plan has {len(raw_steps)} steps; the limit is {MAX_STEPS}.")
        raw_steps = raw_steps[:MAX_STEPS]
    raw_steps = _clean_raw_args(raw_steps)
    copied = _copied_from_examples(raw_steps, goal) if goal else set()
    if copied:
        warnings.append(f"Removed {len(copied)} step(s) the model copied from its built-in examples instead of your request.")
        raw_steps = [None if i in copied else s for i, s in enumerate(raw_steps, start=1)]
    if goal:
        raw_steps, grounded = _ground_schedule(raw_steps, goal, now)
        warnings.extend(grounded)
    raw_steps, folded = _fold_compose_into_webpage(raw_steps)
    if folded:
        warnings.append("Optimised: handed the facts straight to build_webpage instead of composing them first.")
    raw_steps, vscode = _fold_vscode(raw_steps, goal)
    if vscode:
        warnings.append("Repaired: opening a folder plus VS Code became “open in VS Code”.")

    for position, raw_step in enumerate(raw_steps, start=1):
        if raw_step is None:
            continue
        if not isinstance(raw_step, dict):
            errors.append(f"Step {position} is not an object.")
            continue
        action = PLANNER_CATALOG.get(str(raw_step.get("action", "")).strip())
        if action is None:
            errors.append(f"Step {position} uses unknown action {raw_step.get('action')!r}.")
            continue
        raw_args = raw_step.get("args") if isinstance(raw_step.get("args"), dict) else {}
        args = {key: canonical_templates(value) for key, value in action.canonical_args(raw_args).items()}
        for key in _SCALAR_FIELDS & args.keys():
            if isinstance(args[key], str):
                args[key] = re.sub(r"\{\{step\d+\}\}", "", args[key]).strip()
        bad_refs = [
            n for value in args.values() for n in referenced_steps(value) if not 1 <= n < position
        ]
        if bad_refs:
            errors.append(f"Step {position} ({action.name}) refers to step {bad_refs[0]}, which does not come before it.")
            continue
        dropped_refs = [n for value in args.values() for n in referenced_steps(value) if n not in index_map]
        if dropped_refs:
            errors.append(f"Step {position} ({action.name}) depends on step {dropped_refs[0]}, which was rejected.")
            continue
        args = {
            key: re.sub(r"\{\{step(\d+)\}\}", lambda m: "{{step" + str(index_map[int(m.group(1))]) + "}}", value)
            if isinstance(value, str) else value
            for key, value in args.items()
        }
        placeholder = next(
            (m.group(0) for v in args.values() if isinstance(v, str) for m in [_PLACEHOLDER.search(v)] if m),
            None,
        )
        if placeholder:
            errors.append(f"Step {position} ({action.name}) contains the placeholder {placeholder!r}; write real content.")
            continue
        if (
            action.name == "compose"
            and _SENDS.search(str(args.get("instruction", "")))
            and not _DRAFTING.search(goal)
        ):
            warnings.append(f"Removed step {position}: it tried to perform an out-of-scope request by writing text.")
            continue
        if action.name == "speak" and goal and not _SPOKEN.search(goal):
            warnings.append(f"Removed step {position} (speak): you did not ask for anything to be said aloud.")
            continue
        if action.name == "send_whatsapp" and goal:
            reason = _whatsapp_requested(goal, str(args.get("contact") or args.get("to") or ""))
            if reason:
                warnings.append(f"Removed step {position} (send_whatsapp): {reason}.")
                continue
        try:
            normalized = action.normalize(args, ctx)
        except ArgError as exc:
            errors.append(f"Step {position} ({action.name}): {exc}.")
            continue
        if action.name == "pages_create" and not normalized.get("export_pdf") and re.search(r"\bpdfs?\b", goal, re.I):
            normalized["export_pdf"] = True
            warnings.append("Repaired: you asked for a PDF, so the Pages document will be exported as one too.")
        key = {"open_in_vscode": "path", "open_path": "target"}.get(action.name)
        if key and goal and normalized.get("kind", "path") == "path" and not Path(normalized[key]).exists():
            grounded = ground_path(goal, ctx.known_folders)
            if grounded:
                warnings.append(f"Grounded: the plan named {pretty_path(normalized[key])}, which does not exist; using {pretty_path(grounded)} from your request.")
                normalized[key] = grounded
        steps.append(
            PlannedStep(
                action=action.name,
                args=normalized,
                label=re.sub(r"\{\{step(\d+)\}\}", r"result of step \1", action.describe(normalized)),
                risk=action.risk,
                app=action.app,
                produces_text=action.produces_text,
            )
        )
        index_map[position] = len(steps)

    title = str(raw.get("title") or "Mission").strip()[:60] or "Mission"
    unsupported = str(raw.get("unsupported") or "").strip()[:300]
    return Plan(title=title, steps=steps, unsupported=unsupported, warnings=warnings, errors=errors)


def calendar_hint(now: datetime) -> str:
    days = ", ".join(
        f"{(now + timedelta(days=offset)).strftime('%A %-d %b')}" for offset in range(0, 3)
    )
    return f"(Context: it is {now.strftime('%H:%M')} on {days.split(',')[0]}.)"


def _planned(name: str, args: dict[str, Any], now: datetime) -> PlannedStep:
    action = CATALOG[name]
    normalized = action.normalize(args, PlanContext(now=now))
    return PlannedStep(
        action=name, args=normalized, label=action.describe(normalized),
        risk=action.risk, app=action.app, produces_text=action.produces_text,
    )


def _reply_plan(text: str, now: datetime) -> Plan:
    return Plan(title="Hello", steps=[_planned("reply", {"text": text}, now)], unsupported="")


_DRAFT_JUNK = re.compile(r"\[[^\]]{0,40}\]|^\s*(?:subject|message|whatsapp)\s*:\s*", re.I | re.M)


def clean_draft(text: str) -> str:
    """The model's draft as one sendable line: no quotes, labels or [placeholders]."""
    text = re.sub(r"^```\w*\n?|```$", "", text.strip()).strip()
    text = _DRAFT_JUNK.sub("", text)
    text = " ".join(text.split()).strip(" \"'“”")
    return text[:700]


async def quick_plan(goal: str, now: datetime, on_delta: llm.DeltaCallback | None = None) -> tuple[Plan, list[llm.LLMResult]] | None:
    """Plan small talk and WhatsApp messages with rules instead of the model (None = use the model)."""
    reply = intents.small_talk(goal, now)
    if reply:
        return _reply_plan(reply, now), []
    tool = _tool_plan(goal, now)
    if tool is not None:
        return tool, []
    if not intents.WHATSAPP.search(goal):
        refused = _refusal(goal)
        return (refused, []) if refused else None
    chats = await whatsapp_visible_chats()
    intent = intents.parse_whatsapp(goal, chats)
    if intent is None:
        if intents.missing_recipient(goal):
            return _reply_plan("Who should I send it to? Try: “say hi to Sam on WhatsApp”.", now), []
        return None

    runs: list[llm.LLMResult] = []
    notes: list[str] = []
    message = intent.message
    if intent.compose:
        if on_delta is not None:
            await on_delta(f"Drafting the WhatsApp message for {intent.contact}…\n\n")
        result = await llm.chat(
            [{"role": "system", "content": COMPOSE_SYSTEM}, {"role": "user", "content": intent.compose}],
            on_delta=on_delta, num_predict=220, temperature=0.3,
        )
        runs.append(result)
        message = clean_draft(result.text)
        if not message:
            return _reply_plan("I couldn't draft that message — please try again or tell me the exact words.", now), runs
        notes.append("Drafted the message on-device — read it before approving; nothing is sent until you do.")

    contact = intent.contact
    resolved = pick_chat(contact, chats) if chats else None
    if resolved and resolved != contact:
        notes.append(f"Matched “{contact}” to your WhatsApp chat “{resolved}”.")
        contact = resolved
    elif chats and not resolved:
        notes.append(f"“{contact}” is not among the chats on screen — WhatsApp search will look for it.")
    notes.insert(0, f"Understood instantly: WhatsApp “{_short_text(message)}” to {contact} — names and words taken from your request.")
    step = _planned("send_whatsapp", {"contact": contact, "message": message}, now)
    return Plan(title=f"WhatsApp {contact}", steps=[step], unsupported="", notes=notes), runs


def _refusal(goal: str) -> Plan | None:
    """A goal made only of things no audited action can do is refused instantly, not "worked around"."""
    clauses = [c for c in re.split(r"\s*(?:[,;]|\band\b|\bthen\b|\balso\b)\s*", goal, flags=re.I) if len(c.split()) >= 2]
    if not clauses:
        return None
    categories: list[str] = []
    for clause in clauses:
        category = next((cat for pattern, cat, _stems in OUT_OF_SCOPE if re.search(pattern, clause, re.I)), None)
        if category is None:
            return None
        if category not in categories:
            categories.append(category)
    listed = ", ".join([categories[0]] + [c.lower() for c in categories[1:]])
    return Plan(
        title="Out of scope",
        steps=[],
        unsupported=f"Not allowed: {listed}. These are outside Human Emulator's audited actions, so nothing was done.",
        notes=["Refused instantly — no model call, nothing touched."],
    )


_WORKAROUND_APPS = re.compile(r"\b(mail|outlook|gmail|spark|airmail|messages|safari|chrome|firefox|brave|swiggy|zomato|amazon)\b", re.I)


def _drop_workarounds(goal: str, plan: Plan) -> None:
    """When part of a goal is out of scope, the model likes to fake it: draft the email, open Mail.
    Drop those stand-ins (and renumber {{stepN}} references) so the plan only does what it really can."""
    if not plan.unsupported:
        return
    referenced = {n for step in plan.steps for value in step.args.values() for n in referenced_steps(value)}
    drop: set[int] = set()
    for position, step in enumerate(plan.steps, start=1):
        if step.action == "open_app" and _WORKAROUND_APPS.search(str(step.args.get("app", ""))) and not re.search(r"\bopen\b", goal, re.I):
            drop.add(position)
        elif step.action == "compose" and position not in referenced and not _DRAFTING.search(goal):
            drop.add(position)
    if not drop:
        return
    new_index = {}
    kept: list[PlannedStep] = []
    for position, step in enumerate(plan.steps, start=1):
        if position in drop:
            continue
        kept.append(step)
        new_index[position] = len(kept)
    for step in kept:
        step.args = {
            key: re.sub(r"\{\{step(\d+)\}\}", lambda m: "{{step" + str(new_index.get(int(m.group(1)), 0)) + "}}", value)
            if isinstance(value, str) else value
            for key, value in step.args.items()
        }
    plan.steps = kept
    plan.warnings.append(f"Removed {len(drop)} step(s) that only imitated the out-of-scope request.")


def _short_text(text: str, limit: int = 50) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tool_plan(goal: str, now: datetime) -> Plan | None:
    """Calculator, "press X in Y" and "describe my screen": parsed by rules, planned instantly."""
    expression = intents.parse_calculation(goal)
    press = intents.parse_press(goal)
    describe = intents.parse_describe(goal)
    pages = intents.parse_pages(goal)
    try:
        if pages:
            article = "an" if re.match(r"[aeiou]", pages.kind) and not pages.kind.startswith(("one", "uni", "use")) else "a"
            length = "about 350 words" if "page" in pages.kind else "about 300-450 words"
            write = _planned("compose", {
                "instruction": (
                    f"Write {article} {pages.kind} about {pages.topic}, {length}. Do not repeat the title. "
                    "Use '# ' for 2-4 short section headings and plain paragraphs under them. "
                    "No bold, no dates, no placeholders."
                ),
                "input": "",
            }, now)
            doc = _planned("pages_create", {"title": pages.title, "content": "{{step1}}", "export_pdf": pages.pdf}, now)
            doc.label = re.sub(r"\{\{step(\d+)\}\}", r"result of step \1", doc.label)
            return Plan(title=pages.title[:40], steps=[write, doc], unsupported="",
                        notes=[f"Understood instantly: {article} {pages.kind} in Pages" + (" with a PDF copy." if pages.pdf else ".")])
        if expression:
            step = _planned("calculate", {"expression": expression}, now)
            return Plan(title="Calculator", steps=[step], unsupported="",
                        notes=[f"Understood instantly: {intents.pretty_expression(expression)} in Calculator."])
        if press:
            step = _planned("press_button", {"button": press[0], "app": press[1]}, now)
            return Plan(title=f"Press in {step.args['app']}", steps=[step], unsupported="",
                        notes=["Any app: Accessibility tree first, on-device OCR only if the button is not in the tree."])
        if describe is not None:
            step = _planned("describe_screen", {"app": describe}, now)
            return Plan(title="Describe my screen", steps=[step], unsupported="")
    except ArgError as exc:
        return _reply_plan(f"I can't do that: {exc}.", now)
    return None


async def _ground_whatsapp_contacts(plan: Plan) -> None:
    """Model-planned WhatsApp steps: match the contact to a real chat name when there is exactly one."""
    steps = [s for s in plan.steps if s.action == "send_whatsapp"]
    if not steps:
        return
    chats = await whatsapp_visible_chats()
    for step in steps:
        resolved = pick_chat(step.args["contact"], chats) if chats else None
        if resolved and resolved != step.args["contact"]:
            plan.notes.append(f"Matched “{step.args['contact']}” to your WhatsApp chat “{resolved}”.")
            step.args["contact"] = resolved
            step.label = CATALOG["send_whatsapp"].describe(step.args)


async def make_plan(goal: str, now: datetime, on_delta: llm.DeltaCallback | None = None) -> tuple[Plan, list[llm.LLMResult]]:
    quick = await quick_plan(goal, now, on_delta)
    if quick is not None:
        return quick

    messages = base_messages(f"{goal}\n{calendar_hint(now)}")
    runs: list[llm.LLMResult] = []

    result = await llm.chat(messages, schema=PLAN_SCHEMA, on_delta=on_delta, num_predict=700)
    runs.append(result)
    raw = _parse(result.text)
    plan = validate(raw, now, goal)

    if plan.errors:
        if on_delta is not None:
            await on_delta("\n\n— validator rejected a step, re-planning —\n\n")
        messages += [
            {"role": "assistant", "content": result.text},
            {
                "role": "user",
                "content": "Fix these problems and return the full corrected plan:\n- " + "\n- ".join(plan.errors),
            },
        ]
        retry = await llm.chat(messages, schema=PLAN_SCHEMA, on_delta=on_delta, num_predict=700)
        runs.append(retry)
        repaired = validate(_parse(retry.text), now, goal)
        if len(repaired.errors) < len(plan.errors) and repaired.steps:
            repaired.warnings.append("Plan was repaired once after Human Emulator's validator rejected a step.")
            plan = repaired

    if plan.errors:
        plan.warnings.extend(f"Dropped: {error}" for error in plan.errors)
    flag_out_of_scope(goal, plan)
    _drop_workarounds(goal, plan)
    await _ground_whatsapp_contacts(plan)
    return plan, runs


def flag_out_of_scope(goal: str, plan: Plan) -> None:
    """Name requested capabilities that no planned step covers, if the model did not."""
    lowered = goal.lower()
    covered = json.dumps([step.args for step in plan.steps]).lower()
    explained = plan.unsupported.lower()
    missing: list[str] = []
    for pattern, category, stems in OUT_OF_SCOPE:
        match = re.search(pattern, lowered)
        if not match or match.group(0) in covered:
            continue
        if match.group(0) in explained or any(stem in explained for stem in stems):
            continue
        missing.append(category)
    if missing:
        sentence = f"{' and '.join(missing)} is outside Human Emulator's audited actions, so that part was skipped."
        plan.unsupported = f"{plan.unsupported} {sentence}".strip()


def _parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}
