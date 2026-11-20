"""The audited action catalog.

The on-device model never writes code. It can only pick from these actions.
Each action validates its own arguments, performs the change, independently
reads the result back to verify it, and (when possible) knows how to undo it.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import intents
import llm
import timeparse
from macos import (
    DATE_HANDLER,
    HOME,
    UNDO_DIR,
    AXError,
    MacError,
    ax,
    bring_to_front,
    capture_screen,
    click_menu,
    date_args,
    frontmost_app,
    osascript,
    permission_help,
    permissions,
    recognize_text,
    run,
)

ALLOWED_ROOTS = {
    "desktop": HOME / "Desktop",
    "documents": HOME / "Documents",
    "downloads": HOME / "Downloads",
}
ROOT_ALIASES = {"docs": "documents", "download": "downloads"}
WEBPAGE_DIR = HOME / "Documents" / "Human Emulator Missions"
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".html", ".htm"}
MAX_TEXT = 6000

READ = "read"
REVERSIBLE = "reversible"
ONE_WAY = "one-way"


class ArgError(ValueError):
    """The plan gave an action unusable arguments."""


@dataclass
class PlanContext:
    now: datetime
    known_folders: list[Path] = field(default_factory=list)
    # Paths earlier steps in this plan will create; never "corrected" to something else.
    created: list[Path] = field(default_factory=list)


@dataclass
class RunContext:
    mission_id: str
    step_index: int
    now: datetime
    known_folders: list[Path]
    emit: Callable[[str, str], Awaitable[None]]
    on_delta: Callable[[str], Awaitable[None]] | None = None
    goal: str = ""


@dataclass
class StepResult:
    output: str = ""
    evidence: str = ""
    undo: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    data: dict[str, Any] | None = None


@dataclass
class Check:
    ok: bool
    evidence: str


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------


def _text(args: dict[str, Any], key: str, *, required: bool = True, limit: int = MAX_TEXT) -> str:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ArgError(f"missing {key}")
        return ""
    if not isinstance(value, (str, int, float, bool)):
        raise ArgError(f"{key} must be text")
    text = str(value).strip()
    return text[:limit]


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int(args: dict[str, Any], key: str, default: int | None, low: int, high: int) -> int:
    value = args.get(key, default)
    if value in (None, ""):
        if default is None:
            raise ArgError(f"missing {key}")
        value = default
    try:
        number = int(round(float(str(value).strip().rstrip("%"))))
    except ValueError as exc:
        raise ArgError(f"{key} must be a number") from exc
    return max(low, min(high, number))


def _bool(args: dict[str, Any], key: str) -> bool:
    value = args.get(key)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "on", "dark", "1", "enable", "enabled"}:
        return True
    if text in {"false", "no", "off", "light", "0", "disable", "disabled"}:
        return False
    raise ArgError(f"{key} must be true or false")


def _real(path: Path) -> Path:
    """Resolve symlinks of the longest existing ancestor (path itself may not exist)."""
    existing = path
    tail: list[str] = []
    while not existing.exists() and existing != existing.parent:
        tail.append(existing.name)
        existing = existing.parent
    resolved = existing.resolve()
    for part in reversed(tail):
        resolved = resolved / part
    return resolved


def _clean_part(part: str) -> str:
    part = part.strip().replace(":", "-")
    part = re.sub(r"[\x00-\x1f]", "", part)
    return part


def resolve_user_path(raw: str, known_folders: list[Path]) -> Path:
    """Map planner paths onto Desktop/Documents/Downloads, refusing anything else."""
    text = (raw or "").strip().strip("\"'").replace("\\", "/")
    if not text:
        raise ArgError("missing path")
    if text.startswith("~"):
        text = text[1:].lstrip("/")
        absolute = HOME / text
    elif text.startswith("/"):
        absolute = Path(text)
    else:
        absolute = None

    if absolute is not None:
        parts = [p for p in absolute.parts if p not in ("/",)]
        if ".." in parts:
            raise ArgError("paths may not contain '..'")
        candidate = Path(os.path.normpath(absolute))
    else:
        parts = [_clean_part(p) for p in text.split("/") if p.strip() not in ("", ".")]
        if not parts:
            raise ArgError("missing path")
        if any(p == ".." for p in parts):
            raise ArgError("paths may not contain '..'")
        first = parts[0].lower()
        first = ROOT_ALIASES.get(first, first)
        if first in ALLOWED_ROOTS:
            candidate = ALLOWED_ROOTS[first].joinpath(*parts[1:])
        else:
            parent = next((f for f in reversed(known_folders) if f.name.lower() == first), None)
            if parent is not None:
                candidate = parent.joinpath(*parts[1:])
            else:
                candidate = ALLOWED_ROOTS["desktop"].joinpath(*parts)

    real = _real(candidate)
    roots = [root.resolve() for root in ALLOWED_ROOTS.values()]
    if real in roots:
        raise ArgError("choose a name inside Desktop, Documents or Downloads")
    if not any(root in real.parents for root in roots):
        raise ArgError("Human Emulator only creates things inside Desktop, Documents or Downloads")
    return candidate


def pretty_path(path: Path | str) -> str:
    text = str(path)
    home = str(HOME)
    return "~" + text[len(home):] if text.startswith(home) else text


_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", "site-packages"}
# Opening these runs code. They are never substituted for a name and never opened.
EXECUTABLE_SUFFIXES = {
    ".app", ".command", ".sh", ".zsh", ".bash", ".tool", ".scpt", ".applescript", ".workflow",
    ".pkg", ".mpkg", ".dmg", ".terminal", ".py", ".rb", ".pl", ".jar", ".exe", ".bat", ".action",
}


def is_executable(path: Path) -> bool:
    return path.suffix.lower() in EXECUTABLE_SUFFIXES or (path.is_file() and os.access(path, os.X_OK))


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _match_rank(wanted: str, name: str) -> int:
    """0 = same name ignoring case/punctuation, >0 = looser containment, -1 = no match."""
    squashed, stem = _squash(name), _squash(Path(name).stem)
    if wanted in (squashed, stem):
        return 0
    if len(wanted) >= 4 and (wanted in squashed or (len(squashed) >= 4 and squashed in wanted)):
        return 1 + abs(len(squashed) - len(wanted))
    return -1


def _spotlight(root: Path, name: str) -> list[Path]:
    """Spotlight still answers when macOS privacy settings stop us listing a folder."""
    words = sorted(re.findall(r"[A-Za-z0-9]{3,}", name), key=len, reverse=True)
    if not words:
        return []
    try:
        out = subprocess.run(
            ["mdfind", "-onlyin", str(root), "-name", words[0]], capture_output=True, text=True, timeout=4
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line) for line in out.splitlines()[:500] if line.strip()]


def locate_user_path(
    raw: str, known_folders: list[Path], *, created: list[Path] | None = None, max_depth: int = 3, budget: int = 20000
) -> Path:
    """resolve_user_path, but forgiving about spelling of existing names.

    "Desktop/final year project" finds ~/Desktop/final-year-Project. Only names the
    model wrote are corrected: absolute paths (an approved plan being replayed) and
    paths an earlier step creates are kept exactly. If nothing matches, the resolved
    (non-existent) path is returned unchanged."""
    path = resolve_user_path(raw, known_folders)
    if path.exists() or str(raw or "").strip().startswith("/"):
        return path
    planned = [*known_folders, *(created or [])]
    if any(path == p or p in path.parents for p in planned):
        return path
    wanted = _squash(path.name)
    if len(wanted) < 3:
        return path
    best: tuple[int, int, Path] | None = None

    def consider(candidate: Path, depth: int) -> None:
        nonlocal best
        if candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
            return
        rank = _match_rank(wanted, candidate.name)
        if rank >= 0 and (best is None or (rank, depth) < best[:2]):
            best = (rank, depth, candidate)

    queue = [(root, 0) for root in ALLOWED_ROOTS.values() if root.is_dir()]
    blocked: list[Path] = []
    seen = 0
    while queue and seen < budget:
        folder, depth = queue.pop(0)
        try:
            entries = list(os.scandir(folder))
        except PermissionError:
            if depth == 0:
                blocked.append(folder)
            continue
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            consider(Path(entry.path), depth)
            if entry.is_dir(follow_symlinks=False) and depth + 1 < max_depth:
                queue.append((Path(entry.path), depth + 1))
    for root in blocked:
        for hit in _spotlight(root, path.name):
            parts = hit.relative_to(root).parts if hit.is_relative_to(root) else ()
            if parts and len(parts) <= max_depth and not any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
                consider(hit, len(parts) - 1)
    return resolve_user_path(str(best[2]), known_folders) if best else path


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 100):
        candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ArgError(f"{pretty_path(path)} already exists")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_parents(path: Path) -> list[str]:
    """Create missing parent folders and return the ones created (deepest last)."""
    missing: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for folder in reversed(missing):
        folder.mkdir()
    return [str(folder) for folder in reversed(missing)]


def _remove_created_folders(folders: list[str]) -> list[str]:
    """Remove folders the agent created if they are empty; return the ones kept."""
    kept: list[str] = []
    for folder in sorted(folders, key=lambda f: f.count("/"), reverse=True):
        path = Path(folder)
        if not path.exists():
            continue
        leftovers = [p for p in path.iterdir() if p.name != ".DS_Store"]
        if leftovers:
            kept.append(folder)
            continue
        (path / ".DS_Store").unlink(missing_ok=True)
        path.rmdir()
    return kept


def _move_to_undo_bin(path: Path, mission_id: str, step_index: int) -> Path:
    target_dir = UNDO_DIR / mission_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique(target_dir / f"{step_index + 1}-{path.name}")
    shutil.move(str(path), str(target))
    return target


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_when_arg(args: dict[str, Any], ctx_now: datetime, required: bool) -> dict[str, Any]:
    phrase = _text(args, "when", required=required, limit=80)
    if not phrase:
        return {}
    try:
        parsed = timeparse.parse_when(phrase, ctx_now)
    except ValueError as exc:
        raise ArgError(str(exc)) from exc
    return {"when": phrase, "at": _iso(parsed.value), "has_time": parsed.has_time}


def _at(args: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(args["at"])


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Action:
    name = ""
    label = ""
    summary = ""
    risk = READ
    app: str | None = None
    produces_text = False
    # macOS permissions the step cannot work without ("accessibility", "screen"); checked before approval.
    requires: tuple[str, ...] = ()
    # Hidden from the model's menu; only Human Emulator's own fast paths plan these.
    planner_visible = True
    # Not an audited skill at all (e.g. the instant "reply"); hidden from the skills list too.
    internal = False
    aliases: dict[str, str] = {}
    # (name, JSON type, required) in the order the model must write them. Constrained
    # decoding fixes property order, so this order has to match natural phrasing.
    params: tuple[tuple[str, str, bool], ...] = ()

    @property
    def signature(self) -> str:
        names = [name if required else f"{name}?" for name, _type, required in self.params]
        return "{" + ", ".join(names) + "}"

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [self.name]},
                "args": {
                    "type": "object",
                    "properties": {name: {"type": kind} for name, kind, _ in self.params},
                    "required": [name for name, _kind, required in self.params if required],
                },
            },
            "required": ["action", "args"],
        }

    def canonical_args(self, raw: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            canonical = self.aliases.get(str(key).strip().lower(), str(key).strip().lower())
            if canonical not in args or args[canonical] in ("", None):
                args[canonical] = value
        return args

    def normalize(self, args: dict[str, Any], ctx: PlanContext) -> dict[str, Any]:
        return {}

    def describe(self, args: dict[str, Any]) -> str:
        return self.label

    async def run(self, args: dict[str, Any], ctx: RunContext) -> StepResult:
        raise NotImplementedError

    async def verify(self, args: dict[str, Any], result: StepResult, ctx: RunContext) -> Check:
        return Check(True, result.evidence or "Read-only step completed.")

    async def undo(self, data: dict[str, Any], ctx: RunContext) -> Check:
        return Check(False, "This step cannot be undone.")

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "signature": self.signature,
            "summary": self.summary,
            "risk": self.risk,
            "app": self.app,
        }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class CreateFolder(Action):
    name = "create_folder"
    label = "Create folder"
    params = (("path", "string", True),)
    summary = "make a folder"
    risk = REVERSIBLE
    app = "Finder"
    aliases = {"folder": "path", "name": "path", "location": "path", "directory": "path"}

    def normalize(self, args, ctx):
        path = resolve_user_path(_text(args, "path", limit=300), ctx.known_folders)
        ctx.known_folders.append(path)
        return {"path": str(path)}

    def describe(self, args):
        return f"Create folder {pretty_path(args['path'])}"

    async def run(self, args, ctx):
        path = resolve_user_path(args["path"], ctx.known_folders)
        ctx.known_folders.append(path)
        if path.exists():
            if not path.is_dir():
                raise MacError(f"{pretty_path(path)} exists and is not a folder")
            return StepResult(
                output=str(path),
                evidence="Folder already existed — left untouched, nothing to undo.",
                artifact={"kind": "path", "path": str(path)},
            )
        created = _make_parents(path / "placeholder")
        return StepResult(
            output=str(path),
            evidence=f"Created {pretty_path(path)}",
            undo={"folders": created},
            artifact={"kind": "path", "path": str(path)},
        )

    async def verify(self, args, result, ctx):
        path = Path(result.output)
        if path.is_dir():
            return Check(True, f"Finder check: {pretty_path(path)} exists and is a folder.")
        return Check(False, f"{pretty_path(path)} was not found after creation.")

    async def undo(self, data, ctx):
        kept = _remove_created_folders(data.get("folders", []))
        if kept:
            return Check(False, f"Kept {pretty_path(kept[0])} because it now contains other files.")
        return Check(True, "Folder removed — it was empty.")


class WriteFile(Action):
    name = "write_file"
    label = "Write file"
    params = (("path", "string", True), ("content", "string", True))
    summary = "create a text/markdown/csv/html file with real content"
    risk = REVERSIBLE
    app = "Finder"
    aliases = {
        "file": "path", "filename": "path", "name": "path", "location": "path",
        "text": "content", "body": "content", "contents": "content", "data": "content",
    }

    def normalize(self, args, ctx):
        path = resolve_user_path(_text(args, "path", limit=300), ctx.known_folders)
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            path = path.with_name(path.name + ".md")
        ctx.created.append(path)
        content = _text(args, "content", limit=20000)
        return {"path": str(path), "content": content}

    def describe(self, args):
        return f"Write {pretty_path(args['path'])}"

    async def run(self, args, ctx):
        path = resolve_user_path(args["path"], ctx.known_folders)
        path = _unique(path)
        created = _make_parents(path)
        data = (args["content"].rstrip() + "\n").encode("utf-8")
        with open(path, "xb") as handle:
            handle.write(data)
        return StepResult(
            output=args["content"],
            evidence=f"Wrote {len(data)} bytes to {pretty_path(path)}",
            undo={"path": str(path), "sha": _sha(data), "folders": created},
            artifact={"kind": "file", "path": str(path)},
            data={"path": str(path), "sha": _sha(data)},
        )

    async def verify(self, args, result, ctx):
        path = Path(result.data["path"])
        if not path.is_file():
            return Check(False, f"{pretty_path(path)} is missing.")
        if _sha(path.read_bytes()) != result.data["sha"]:
            return Check(False, "File contents differ from what Human Emulator wrote.")
        return Check(True, f"Read back {pretty_path(path)} — SHA-256 matches ({result.data['sha'][:10]}…).")

    async def undo(self, data, ctx):
        path = Path(data["path"])
        if not path.exists():
            _remove_created_folders(data.get("folders", []))
            return Check(True, "File was already gone.")
        if _sha(path.read_bytes()) != data["sha"]:
            return Check(False, f"Kept {pretty_path(path)} because it was edited after Human Emulator wrote it.")
        moved = _move_to_undo_bin(path, ctx.mission_id, ctx.step_index)
        _remove_created_folders(data.get("folders", []))
        return Check(True, f"Moved to the undo bin ({pretty_path(moved)}).")


class OpenPath(Action):
    name = "open_path"
    label = "Open"
    params = (("target", "string", True),)
    summary = "open a file, folder or https URL"
    risk = ONE_WAY
    aliases = {"path": "target", "url": "target", "file": "target", "folder": "target", "link": "target"}

    def normalize(self, args, ctx):
        target = _text(args, "target", limit=500)
        if re.match(r"^https?://", target, re.I):
            if not target.lower().startswith("https://"):
                raise ArgError("only https links can be opened")
            return {"target": target, "kind": "url"}
        path = locate_user_path(target, ctx.known_folders, created=ctx.created)
        if is_executable(path):
            raise ArgError("Human Emulator does not open apps or scripts with open_path")
        return {"target": str(path), "kind": "path"}

    def describe(self, args):
        return f"Open {args['target'] if args['kind'] == 'url' else pretty_path(args['target'])}"

    async def run(self, args, ctx):
        target = args["target"]
        if args["kind"] == "path":
            # Exactly the approved path — never re-searched at run time.
            path = resolve_user_path(target, ctx.known_folders)
            if not path.exists():
                raise MacError(f"{pretty_path(path)} does not exist")
            if is_executable(path):
                raise MacError(f"{pretty_path(path)} is an app or script, so Human Emulator will not open it")
            target = str(path)
        code, _out, err = await run(["open", target], timeout=15)
        if code != 0:
            raise MacError(err.strip() or "open failed")
        return StepResult(evidence=f"macOS accepted the open request for {_short(target, 80)}.")


# ---------------------------------------------------------------------------
# Apple apps (Reminders, Calendar, Notes)
# ---------------------------------------------------------------------------

REMINDER_CREATE = """
on run argv
\tset theTitle to item 1 of argv
\tset theNotes to item 2 of argv
\tset dueDate to missing value
\tif (item 3 of argv) is "1" then set dueDate to my mkdate(item 4 of argv, item 5 of argv, item 6 of argv, item 7 of argv, item 8 of argv)
\ttell application "Reminders"
\t\tset theList to default list
\t\ttell theList
\t\t\tset r to make new reminder with properties {name:theTitle}
\t\tend tell
\t\tif theNotes is not "" then set body of r to theNotes
\t\tif dueDate is not missing value then set due date of r to dueDate
\t\treturn (id of r) & linefeed & (name of theList)
\tend tell
end run
""" + DATE_HANDLER

REMINDER_READ = """
on run argv
\ttell application "Reminders"
\t\tset r to reminder id (item 1 of argv)
\t\tset d to due date of r
\t\tset dueText to ""
\t\tif d is not missing value then set dueText to (d as «class isot» as string)
\t\treturn (name of r) & linefeed & dueText
\tend tell
end run
"""

REMINDER_DELETE = """
on run argv
\ttell application "Reminders"
\t\tdelete reminder id (item 1 of argv)
\t\treturn (exists reminder id (item 1 of argv)) as string
\tend tell
end run
"""

EVENT_CREATE = """
on run argv
\tset theTitle to item 1 of argv
\tset theNotes to item 2 of argv
\tset mins to (item 3 of argv) as integer
\tset preferred to item 4 of argv
\tset startDate to my mkdate(item 5 of argv, item 6 of argv, item 7 of argv, item 8 of argv, item 9 of argv)
\tset endDate to startDate + (mins * minutes)
\ttell application "Calendar"
\t\tset targetCal to missing value
\t\tset wanted to {"Home", "Personal", "Calendar", "Work"}
\t\tif preferred is not "" then set wanted to {preferred} & wanted
\t\trepeat with wantedName in wanted
\t\t\trepeat with c in calendars
\t\t\t\ttry
\t\t\t\t\tif (name of c) is (contents of wantedName) and (writable of c) then
\t\t\t\t\t\tset targetCal to contents of c
\t\t\t\t\t\texit repeat
\t\t\t\t\tend if
\t\t\t\tend try
\t\t\tend repeat
\t\t\tif targetCal is not missing value then exit repeat
\t\tend repeat
\t\tif targetCal is missing value then
\t\t\trepeat with c in calendars
\t\t\t\ttry
\t\t\t\t\tif writable of c then
\t\t\t\t\t\tset targetCal to contents of c
\t\t\t\t\t\texit repeat
\t\t\t\t\tend if
\t\t\t\tend try
\t\t\tend repeat
\t\tend if
\t\tif targetCal is missing value then error "No writable calendar found."
\t\ttell targetCal
\t\t\tset e to make new event at end with properties {summary:theTitle, start date:startDate, end date:endDate}
\t\tend tell
\t\tif theNotes is not "" then set description of e to theNotes
\t\treturn (uid of e) & linefeed & (name of targetCal)
\tend tell
end run
""" + DATE_HANDLER

EVENT_READ = """
on run argv
\ttell application "Calendar"
\t\ttell calendar (item 2 of argv)
\t\t\tset matches to (every event whose uid is (item 1 of argv))
\t\t\tif (count of matches) is 0 then return "missing"
\t\t\tset e to item 1 of matches
\t\t\treturn (summary of e) & linefeed & ((start date of e) as «class isot» as string) & linefeed & ((end date of e) as «class isot» as string)
\t\tend tell
\tend tell
end run
"""

EVENT_DELETE = """
on run argv
\ttell application "Calendar"
\t\ttell calendar (item 2 of argv)
\t\t\tset matches to (every event whose uid is (item 1 of argv))
\t\t\trepeat with e in matches
\t\t\t\tdelete e
\t\t\tend repeat
\t\t\treturn (count of (every event whose uid is (item 1 of argv))) as string
\t\tend tell
\tend tell
end run
"""

AGENDA_READ = """
on run argv
\tset d0 to my mkdate(item 1 of argv, item 2 of argv, item 3 of argv, 0, 0)
\tset d1 to d0 + (1 * days)
\tset rows to {}
\ttell application "Calendar"
\t\trepeat with c in calendars
\t\t\tset calName to name of c
\t\t\ttry
\t\t\t\tset evs to (every event of c whose start date < d1 and end date > d0)
\t\t\t\trepeat with e in evs
\t\t\t\t\tset end of rows to ((start date of e) as «class isot» as string) & tab & ((end date of e) as «class isot» as string) & tab & ((allday event of e) as string) & tab & calName & tab & (summary of e)
\t\t\t\tend repeat
\t\t\tend try
\t\tend repeat
\t\tset calendarCount to (count of calendars) as string
\tend tell
\tset AppleScript's text item delimiters to linefeed
\treturn calendarCount & linefeed & (rows as string)
end run
""" + DATE_HANDLER

NOTE_CREATE = """
on run argv
\ttell application "Notes"
\t\tset f to default folder of default account
\t\tset n to make new note at f with properties {body:(item 1 of argv)}
\t\treturn (id of n) & linefeed & (name of n) & linefeed & (name of f)
\tend tell
end run
"""

NOTE_READ = """
on run argv
\ttell application "Notes"
\t\tset n to note id (item 1 of argv)
\t\tset folderName to "Notes"
\t\ttry
\t\t\tset folderName to name of container of n
\t\tend try
\t\treturn (name of n) & linefeed & folderName
\tend tell
end run
"""

NOTE_DELETE = """
on run argv
\ttell application "Notes"
\t\tdelete note id (item 1 of argv)
\t\ttry
\t\t\treturn name of container of note id (item 1 of argv)
\t\ton error
\t\t\treturn "gone"
\t\tend try
\tend tell
end run
"""


class CreateReminder(Action):
    name = "create_reminder"
    label = "Create reminder"
    params = (("title", "string", True), ("when", "string", True), ("notes", "string", False))
    summary = "add an Apple Reminders reminder"
    risk = REVERSIBLE
    app = "Reminders"
    aliases = {
        "name": "title", "task": "title", "text": "title", "reminder": "title",
        "time": "when", "due": "when", "at": "when", "date": "when", "due_date": "when",
        "body": "notes", "details": "notes", "content": "notes", "note": "notes",
    }

    def normalize(self, args, ctx):
        result = {"title": _text(args, "title", limit=200), "notes": _text(args, "notes", required=False)}
        result.update(_parse_when_arg(args, ctx.now, required=False))
        return result

    def describe(self, args):
        when = f" · {timeparse.describe(_at(args))}" if args.get("at") else ""
        return f"Reminder “{_short(args['title'])}”{when}"

    async def run(self, args, ctx):
        due = date_args(_at(args)) if args.get("at") else ["0"] * 5
        out = await osascript(
            REMINDER_CREATE, args["title"], args.get("notes", ""), "1" if args.get("at") else "0", *due,
            app="Reminders",
        )
        reminder_id, list_name = (out.split("\n") + [""])[:2]
        return StepResult(
            evidence=f"Reminders returned id {reminder_id[-12:]} in list “{list_name}”.",
            undo={"id": reminder_id},
            data={"id": reminder_id, "list": list_name},
        )

    async def verify(self, args, result, ctx):
        out = await osascript(REMINDER_READ, result.data["id"], app="Reminders")
        name, due = (out.split("\n") + [""])[:2]
        if name != args["title"]:
            return Check(False, f"Reminder name reads back as “{name}”.")
        if args.get("at") and not due.startswith(args["at"][:16]):
            return Check(False, f"Due date reads back as {due or 'none'}.")
        detail = f", due {timeparse.describe(_at(args))}" if args.get("at") else ""
        return Check(True, f"Read back from Reminders: “{name}”{detail}.")

    async def undo(self, data, ctx):
        still_there = await osascript(REMINDER_DELETE, data["id"], app="Reminders")
        if still_there == "false":
            return Check(True, "Reminder deleted — confirmed gone from Reminders.")
        return Check(False, "Reminders still reports the reminder.")


class CreateEvent(Action):
    name = "create_event"
    label = "Create calendar event"
    params = (("title", "string", True), ("when", "string", True), ("duration_minutes", "integer", True), ("notes", "string", False))
    summary = "add an Apple Calendar event"
    risk = REVERSIBLE
    app = "Calendar"
    aliases = {
        "name": "title", "summary": "title", "event": "title",
        "start": "when", "time": "when", "date": "when", "at": "when", "start_time": "when",
        "duration": "duration_minutes", "minutes": "duration_minutes", "length": "duration_minutes",
        "body": "notes", "details": "notes", "description": "notes", "content": "notes",
    }

    def normalize(self, args, ctx):
        result = {
            "title": _text(args, "title", limit=200),
            "notes": _text(args, "notes", required=False),
            "duration_minutes": _int(args, "duration_minutes", 60, 5, 720),
        }
        result.update(_parse_when_arg(args, ctx.now, required=True))
        return result

    def describe(self, args):
        return (
            f"Calendar “{_short(args['title'])}” · {timeparse.describe(_at(args))} "
            f"for {args['duration_minutes']} min"
        )

    async def run(self, args, ctx):
        out = await osascript(
            EVENT_CREATE,
            args["title"],
            args.get("notes", ""),
            str(args["duration_minutes"]),
            os.environ.get("CASE_CALENDAR", ""),
            *date_args(_at(args)),
            app="Calendar",
            timeout=90,
        )
        uid, calendar = (out.split("\n") + [""])[:2]
        return StepResult(
            evidence=f"Calendar created event {uid[:8]}… in “{calendar}”.",
            undo={"uid": uid, "calendar": calendar},
            data={"uid": uid, "calendar": calendar},
        )

    async def verify(self, args, result, ctx):
        out = await osascript(EVENT_READ, result.data["uid"], result.data["calendar"], app="Calendar", timeout=90)
        if out == "missing":
            return Check(False, "Event not found when reading Calendar back.")
        title, start, end = (out.split("\n") + ["", ""])[:3]
        expected_end = _at(args) + timedelta(minutes=args["duration_minutes"])
        if title != args["title"] or not start.startswith(args["at"][:16]) or not end.startswith(_iso(expected_end)[:16]):
            return Check(False, f"Calendar reads back “{title}” {start} → {end}.")
        return Check(
            True,
            f"Read back from Calendar “{result.data['calendar']}”: {timeparse.describe(_at(args))} → "
            f"{expected_end.strftime('%-I:%M %p')}.",
        )

    async def undo(self, data, ctx):
        remaining = await osascript(EVENT_DELETE, data["uid"], data["calendar"], app="Calendar", timeout=90)
        if remaining == "0":
            return Check(True, "Event deleted — confirmed gone from Calendar.")
        return Check(False, "Calendar still reports the event.")


class ReadAgenda(Action):
    name = "read_agenda"
    label = "Read agenda"
    params = (("day", "string", True),)
    summary = "read calendar events for a day"
    risk = READ
    app = "Calendar"
    produces_text = True
    aliases = {"date": "day", "when": "day"}

    def normalize(self, args, ctx):
        phrase = _text(args, "day", required=False, limit=60) or "today"
        try:
            day = timeparse.parse_day(phrase, ctx.now)
        except ValueError as exc:
            raise ArgError(str(exc)) from exc
        return {"day": phrase, "date": day.isoformat()}

    def describe(self, args):
        day = datetime.fromisoformat(args["date"])
        return f"Read Calendar for {day.strftime('%A %-d %b')}"

    async def run(self, args, ctx):
        day = datetime.fromisoformat(args["date"])
        out = await osascript(AGENDA_READ, *date_args(day)[:3], app="Calendar", timeout=120)
        lines = out.split("\n")
        calendar_count = lines[0].strip()
        events = []
        for row in lines[1:]:
            parts = row.split("\t")
            if len(parts) < 5:
                continue
            start, end, all_day, calendar, title = parts[0], parts[1], parts[2], parts[3], "\t".join(parts[4:])
            events.append({"start": start, "end": end, "all_day": all_day == "true", "calendar": calendar, "title": title})
        events.sort(key=lambda e: e["start"])
        heading = day.strftime("%A %-d %B")
        if not events:
            text = f"No events on {heading}."
        else:
            rows = []
            for e in events:
                when = "All day" if e["all_day"] else (
                    datetime.fromisoformat(e["start"]).strftime("%-I:%M %p") + "–"
                    + datetime.fromisoformat(e["end"]).strftime("%-I:%M %p")
                )
                rows.append(f"- {when}: {e['title']} ({e['calendar']})")
            text = f"{heading}:\n" + "\n".join(rows)
        return StepResult(
            output=text,
            evidence=f"Read {len(events)} event(s) across {calendar_count} calendars. Recurring events may be omitted.",
            data={"events": events},
        )


def _note_html(title: str, content: str) -> str:
    parts = [f"<h1>{html.escape(title)}</h1>"]
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in content.splitlines():
        stripped = line.strip()
        bullet = re.match(r"^([-*•]|\d+[.)]|- \[[ x]\])\s+(.*)$", stripped)
        if bullet:
            bullets.append(html.escape(bullet.group(2)))
            continue
        flush()
        if stripped.startswith("#"):
            parts.append(f"<h2>{html.escape(stripped.lstrip('#').strip())}</h2>")
        elif stripped:
            parts.append(f"<div>{html.escape(stripped)}</div>")
        else:
            parts.append("<div><br></div>")
    flush()
    return "".join(parts)


class CreateNote(Action):
    name = "create_note"
    label = "Create note"
    params = (("title", "string", True), ("content", "string", True))
    summary = "create an Apple Notes note"
    risk = REVERSIBLE
    app = "Notes"
    aliases = {"name": "title", "heading": "title", "body": "content", "text": "content", "contents": "content"}

    def normalize(self, args, ctx):
        return {"title": _text(args, "title", limit=120), "content": _text(args, "content", limit=20000)}

    def describe(self, args):
        return f"Note “{_short(args['title'])}”"

    async def run(self, args, ctx):
        out = await osascript(NOTE_CREATE, _note_html(args["title"], args["content"]), app="Notes")
        note_id, name, folder = (out.split("\n") + ["", ""])[:3]
        return StepResult(
            evidence=f"Notes created “{name}” in folder “{folder}”.",
            undo={"id": note_id},
            data={"id": note_id, "folder": folder},
        )

    async def verify(self, args, result, ctx):
        out = await osascript(NOTE_READ, result.data["id"], app="Notes")
        name, folder = (out.split("\n") + [""])[:2]
        if name.strip() != args["title"].strip():
            return Check(False, f"Note reads back with title “{name}”.")
        return Check(True, f"Read back from Notes: “{name}” in “{folder}”.")

    async def undo(self, data, ctx):
        where = await osascript(NOTE_DELETE, data["id"], app="Notes")
        if where in {"gone", "Recently Deleted"}:
            return Check(True, "Note moved to Notes → Recently Deleted.")
        return Check(False, f"Note is still in “{where}”.")


# ---------------------------------------------------------------------------
# Perception and generation
# ---------------------------------------------------------------------------


class ReadScreen(Action):
    name = "read_screen"
    label = "Read screen"
    params = ()
    summary = "capture the screen and read its text on-device (OCR)"
    risk = READ
    produces_text = True
    requires = ("screen",)
    aliases = {"delay": "delay_seconds", "seconds": "delay_seconds"}

    def normalize(self, args, ctx):
        return {"delay_seconds": _int(args, "delay_seconds", 4, 0, 10)}

    def describe(self, args):
        return f"Read the screen on-device (after {args['delay_seconds']}s)"

    async def run(self, args, ctx):
        for remaining in range(args["delay_seconds"], 0, -1):
            await ctx.emit("observe", f"Capturing screen in {remaining}… switch to the window you want read.")
            await asyncio.sleep(1)
        name = f"{ctx.mission_id}-{ctx.step_index + 1}"
        image = await capture_screen(name)
        started = time.monotonic()
        result = await recognize_text(image)
        lines = result.get("lines", [])
        text = "\n".join(line["text"] for line in lines)[:MAX_TEXT]
        confidence = sum(line["confidence"] for line in lines) / len(lines) if lines else 0.0
        return StepResult(
            output=text,
            evidence=(
                f"Apple Vision read {len(lines)} lines from a {result['width']}×{result['height']} capture "
                f"in {int((time.monotonic() - started) * 1000)} ms (avg confidence {confidence:.2f})."
            ),
            artifact={"kind": "screenshot", "url": f"/api/captures/{name}.png"},
            data={"lines": len(lines)},
        )

    async def verify(self, args, result, ctx):
        lines = result.data["lines"]
        if lines == 0:
            return Check(False, "No text recognised. Is Screen Recording allowed for the app running Human Emulator?")
        if lines < 6:
            return Check(True, f"Only {lines} lines read — if windows are missing, grant Screen Recording permission.")
        return Check(True, result.evidence)


COMPOSE_SYSTEM = (
    "You are the writing engine inside Human Emulator, running fully on-device. "
    "Follow the instruction exactly. Output only the requested text: no preamble, "
    "no closing remarks, no markdown code fences. Prefer short lines and '- ' bullets."
)


def tidy_markdown(text: str) -> str:
    """Small models love **bold** and "*   " bullets; Notes, Pages and speech need plain text."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"^(\s*)[*•]\s+", r"\1- ", text, flags=re.M)
    return re.sub(r"^(\s*)-\s{2,}", r"\1- ", text, flags=re.M)


class Compose(Action):
    name = "compose"
    label = "Compose"
    params = (("instruction", "string", True), ("input", "string", True))
    summary = "write or summarise text with the on-device model"
    risk = READ
    produces_text = True
    aliases = {
        "task": "instruction", "prompt": "instruction", "goal": "instruction",
        "text": "input", "content": "input", "source": "input", "context": "input",
    }

    def normalize(self, args, ctx):
        return {"instruction": _text(args, "instruction", limit=600), "input": _text(args, "input", required=False)}

    def describe(self, args):
        return f"Compose: {_short(args['instruction'], 70)}"

    async def run(self, args, ctx):
        user = args["instruction"]
        if args.get("input"):
            user += "\n\nSource material:\n" + args["input"][:MAX_TEXT]
        result = await llm.chat(
            [{"role": "system", "content": COMPOSE_SYSTEM}, {"role": "user", "content": user}],
            on_delta=ctx.on_delta,
            num_predict=450,
            temperature=0.3,
        )
        text = tidy_markdown(re.sub(r"^```\w*\n?|```$", "", result.text.strip()).strip())
        return StepResult(
            output=text,
            evidence=f"On-device model wrote {result.tokens} tokens at {result.tokens_per_s} tok/s.",
            data={"tokens": result.tokens, "tokens_per_s": result.tokens_per_s},
        )

    async def verify(self, args, result, ctx):
        if not result.output.strip():
            return Check(False, "The model returned no text.")
        return Check(True, result.evidence)


WEBPAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tagline": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "body", "points"],
            },
        },
        "closing": {"type": "string"},
    },
    "required": ["title", "tagline", "sections", "closing"],
}

WEBPAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#0b0d0a;--card:#13160f;--line:#262b1d;--text:#eef3e6;--muted:#9aa58b;--accent:#d4ff3a}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1d2a0c 0,transparent 60%),var(--bg);color:var(--text);font:17px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:72px 28px 96px}}
.eyebrow{{font:600 12px ui-monospace,"SF Mono",monospace;letter-spacing:.18em;color:var(--accent);text-transform:uppercase}}
h1{{font-size:clamp(44px,7vw,84px);line-height:1;margin:18px 0 18px;letter-spacing:-.03em}}
.tagline{{font-size:21px;color:var(--muted);max-width:680px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px;margin-top:56px}}
section{{background:linear-gradient(180deg,#171b12,var(--card));border:1px solid var(--line);border-radius:22px;padding:26px}}
h2{{margin:0 0 10px;font-size:21px}}section p{{color:#c8d1bb;margin:0 0 14px}}
ul{{margin:0;padding:0;list-style:none}}li{{padding:7px 0 7px 22px;position:relative;border-top:1px solid var(--line)}}
li:before{{content:"";position:absolute;left:0;top:16px;width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}}
.closing{{margin-top:48px;padding:28px;border-radius:22px;border:1px solid #3d4a1a;background:#151b0b;font-size:20px}}
footer{{margin-top:36px;color:var(--muted);font:12px ui-monospace,"SF Mono",monospace}}
</style></head><body><main>
<div class="eyebrow">Built on-device by Human Emulator</div>
<h1>{title}</h1><p class="tagline">{tagline}</p>
<div class="grid">{sections}</div>
<div class="closing">{closing}</div>
<footer>Generated {generated} · model {model} · no internet required</footer>
</main></body></html>
"""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "page"


WEBPAGE_SYSTEM = (
    "You write website copy inside Human Emulator, running fully on-device. Every field is plain text: "
    "no markdown, no asterisks, no leading dashes or bullet symbols, no links, no square brackets."
)


def _plain(value: Any) -> str:
    """Strip markdown/bullet debris and bracketed placeholders small models leave in fields."""
    text = str(value or "")
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"(\*\*|__|`|#+\s)", "", text)
    text = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", text)
    return " ".join(text.split()).strip(" -")


class BuildWebpage(Action):
    name = "build_webpage"
    label = "Build webpage"
    params = (("topic", "string", True), ("details", "string", True))
    summary = "generate a styled one-page website and open it"
    risk = REVERSIBLE
    app = "Browser"
    aliases = {
        "title": "topic", "subject": "topic", "about": "topic", "name": "topic",
        "brief": "details", "description": "details", "content": "details", "notes": "details",
    }

    def normalize(self, args, ctx):
        return {"topic": _text(args, "topic", limit=200), "details": _text(args, "details", required=False, limit=3000)}

    def describe(self, args):
        return f"Build a webpage about “{_short(args['topic'])}”"

    async def run(self, args, ctx):
        prompt = (
            f"Write the content for a one-page website about: {args['topic']}.\n"
            + (f"The user's own request (authoritative facts): {ctx.goal}\n" if ctx.goal else "")
            + (f"Extra facts: {args['details']}\n" if args.get("details") else "")
            + "Use the real name of the subject as the title. Only state things supported by the facts above; "
            "never invent statistics, certifications, customers or compliance claims.\n"
            "Give exactly 3 sections. Each heading is 2-4 words. Each body is 1-2 sentences. "
            "Each section has 3 short points. The closing is one inspiring sentence. "
            "Be concrete and specific. No placeholders, no links, no lorem ipsum."
        )
        result = await llm.chat(
            [{"role": "system", "content": WEBPAGE_SYSTEM}, {"role": "user", "content": prompt}],
            schema=WEBPAGE_SCHEMA,
            on_delta=ctx.on_delta,
            num_predict=650,
            temperature=0.4,
        )
        try:
            content = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise MacError("The model returned malformed page content.") from exc
        sections = "".join(
            "<section><h2>{}</h2><p>{}</p><ul>{}</ul></section>".format(
                html.escape(_plain(s.get("heading"))),
                html.escape(_plain(s.get("body"))),
                "".join(f"<li>{html.escape(_plain(p))}</li>" for p in (s.get("points") or [])[:5] if _plain(p)),
            )
            for s in (content.get("sections") or [])[:4]
            if isinstance(s, dict) and _plain(s.get("heading"))
        )
        title = _plain(content.get("title")) or args["topic"]
        closing = _plain(content.get("closing"))
        page = WEBPAGE_TEMPLATE.format(
            title=html.escape(title),
            tagline=html.escape(_plain(content.get("tagline"))),
            sections=sections,
            closing=html.escape(closing) if len(closing) > 12 else html.escape(_plain(content.get("tagline"))),
            generated=datetime.now().strftime("%-d %b %Y %H:%M"),
            model=html.escape(llm.MODEL),
        )
        WEBPAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = _unique(WEBPAGE_DIR / f"{_slug(title)}.html")
        data = page.encode("utf-8")
        path.write_bytes(data)
        code, _out, err = await run(["open", str(path)], timeout=15)
        opened = "opened in your browser" if code == 0 else f"could not open ({err.strip()})"
        return StepResult(
            output=title,
            evidence=f"Generated {result.tokens} tokens at {result.tokens_per_s} tok/s, saved {pretty_path(path)}, {opened}.",
            undo={"path": str(path), "sha": _sha(data), "folders": []},
            artifact={"kind": "file", "path": str(path)},
            data={"path": str(path), "sha": _sha(data), "title": title},
        )

    async def verify(self, args, result, ctx):
        path = Path(result.data["path"])
        if not path.is_file():
            return Check(False, "Webpage file is missing.")
        page = path.read_text("utf-8")
        if html.escape(result.data["title"]) not in page or page.count("<section>") < 1:
            return Check(False, "Webpage is missing its title or sections.")
        return Check(True, f"Read back {pretty_path(path)}: {page.count('<section>')} sections, {len(page)//1024} KB.")

    async def undo(self, data, ctx):
        return await WriteFile().undo(data, ctx)


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    HOME / "Applications",
    Path("/System/Library/CoreServices"),
]
APP_ALIASES = {
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "chrome": "Google Chrome", "settings": "System Settings", "system preferences": "System Settings",
    "calendar app": "Calendar", "itunes": "Music", "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "whatsapp": "WhatsApp", "terminal app": "Terminal",
}


def find_app(name: str) -> tuple[str, Path, str]:
    wanted = APP_ALIASES.get(name.strip().lower(), name.strip())
    wanted_lower = wanted.lower().removesuffix(".app")
    for folder in APP_DIRS:
        if not folder.is_dir():
            continue
        for bundle in folder.glob("*.app"):
            if bundle.stem.lower() == wanted_lower:
                try:
                    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
                except (OSError, plistlib.InvalidFileException):
                    continue
                return bundle.stem, bundle, str(info.get("CFBundleIdentifier", ""))
    raise ArgError(f"No app named “{name}” is installed")


APP_RUNNING = 'on run argv\n\treturn (application id (item 1 of argv) is running) as string\nend run\n'
APP_QUIT = 'on run argv\n\ttell application id (item 1 of argv) to quit\nend run\n'


class OpenApp(Action):
    name = "open_app"
    label = "Open app"
    params = (("app", "string", True),)
    summary = "launch a Mac app"
    risk = REVERSIBLE
    aliases = {"name": "app", "application": "app", "target": "app"}

    def normalize(self, args, ctx):
        app_name, bundle, bundle_id = find_app(_text(args, "app", limit=80))
        return {"app": app_name, "bundle": str(bundle), "bundle_id": bundle_id}

    def describe(self, args):
        return f"Open {args['app']}"

    async def run(self, args, ctx):
        was_running = await osascript(APP_RUNNING, args["bundle_id"]) == "true"
        code, _out, err = await run(["open", "-a", args["bundle"]], timeout=20)
        if code != 0:
            raise MacError(err.strip() or f"Could not open {args['app']}")
        for _ in range(20):
            if await osascript(APP_RUNNING, args["bundle_id"]) == "true":
                break
            await asyncio.sleep(0.25)
        return StepResult(
            evidence=f"Launched {args['app']}" + (" (it was already running)." if was_running else "."),
            undo=None if was_running else {"bundle_id": args["bundle_id"], "app": args["app"]},
        )

    async def verify(self, args, result, ctx):
        if await osascript(APP_RUNNING, args["bundle_id"]) == "true":
            return Check(True, f"macOS reports {args['app']} ({args['bundle_id']}) is running.")
        return Check(False, f"{args['app']} is not running.")

    async def undo(self, data, ctx):
        await osascript(APP_QUIT, data["bundle_id"], app=data["app"])
        for _ in range(20):
            if await osascript(APP_RUNNING, data["bundle_id"]) == "false":
                return Check(True, f"Quit {data['app']} — it was not running before the mission.")
            await asyncio.sleep(0.25)
        return Check(False, f"{data['app']} is still running.")


VOLUME_READ = 'set s to get volume settings\nreturn ((output volume of s) as string) & "," & ((output muted of s) as string)\n'
VOLUME_SET = (
    "on run argv\n\tset volume output volume ((item 1 of argv) as integer)\n"
    '\tset volume output muted ((item 2 of argv) is "true")\nend run\n'
)


class SetVolume(Action):
    name = "set_volume"
    label = "Set volume"
    params = (("level", "integer", True),)
    summary = "set output volume 0-100"
    risk = REVERSIBLE
    aliases = {"volume": "level", "value": "level", "percent": "level", "to": "level"}

    def normalize(self, args, ctx):
        return {"level": _int(args, "level", None, 0, 100)}

    def describe(self, args):
        return f"Set volume to {args['level']}%"

    async def _read(self) -> tuple[int, bool]:
        volume, muted = (await osascript(VOLUME_READ)).split(",")
        return int(volume) if volume.isdigit() else 0, muted == "true"

    async def run(self, args, ctx):
        previous, muted = await self._read()
        await osascript(VOLUME_SET, str(args["level"]), "false")
        return StepResult(
            evidence=f"Volume {previous}% → {args['level']}%.",
            undo={"level": previous, "muted": muted},
        )

    async def verify(self, args, result, ctx):
        volume, muted = await self._read()
        if abs(volume - args["level"]) <= 2 and not muted:
            return Check(True, f"Core Audio reads back {volume}% (unmuted).")
        return Check(False, f"Volume reads back {volume}% (muted={muted}).")

    async def undo(self, data, ctx):
        await osascript(VOLUME_SET, str(data["level"]), "true" if data["muted"] else "false")
        volume, _ = await self._read()
        return Check(abs(volume - data["level"]) <= 2, f"Volume restored to {volume}%.")


DARK_READ = 'tell application "System Events" to tell appearance preferences to return (dark mode as string)\n'
DARK_SET = (
    'on run argv\n\ttell application "System Events" to tell appearance preferences to '
    'set dark mode to ((item 1 of argv) is "true")\nend run\n'
)


class SetDarkMode(Action):
    name = "set_dark_mode"
    label = "Set appearance"
    params = (("enabled", "boolean", True),)
    summary = "true = dark mode, false = light mode"
    risk = REVERSIBLE
    app = "System Events"
    aliases = {"dark": "enabled", "on": "enabled", "value": "enabled", "mode": "enabled", "dark_mode": "enabled"}

    def normalize(self, args, ctx):
        return {"enabled": _bool(args, "enabled")}

    def describe(self, args):
        return "Switch to dark mode" if args["enabled"] else "Switch to light mode"

    async def run(self, args, ctx):
        previous = await osascript(DARK_READ, app="System Events") == "true"
        await osascript(DARK_SET, "true" if args["enabled"] else "false", app="System Events")
        return StepResult(
            evidence=f"Appearance {'dark' if previous else 'light'} → {'dark' if args['enabled'] else 'light'}.",
            undo={"enabled": previous},
        )

    async def verify(self, args, result, ctx):
        current = await osascript(DARK_READ, app="System Events") == "true"
        mode = "dark" if current else "light"
        return Check(current == args["enabled"], f"System Events reads back {mode} mode.")

    async def undo(self, data, ctx):
        await osascript(DARK_SET, "true" if data["enabled"] else "false", app="System Events")
        current = await osascript(DARK_READ, app="System Events") == "true"
        return Check(current == data["enabled"], f"Appearance restored to {'dark' if current else 'light'} mode.")


UTF8_ENV = {**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}


async def _pbpaste() -> str:
    proc = await asyncio.create_subprocess_exec(
        "pbpaste", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=UTF8_ENV
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


async def _pbcopy(text: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "pbcopy", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=UTF8_ENV
    )
    await proc.communicate(text.encode("utf-8"))


class CopyToClipboard(Action):
    name = "copy_to_clipboard"
    label = "Copy to clipboard"
    params = (("text", "string", True),)
    summary = "put text on the clipboard"
    risk = REVERSIBLE
    aliases = {"content": "text", "value": "text", "input": "text"}

    def normalize(self, args, ctx):
        return {"text": _text(args, "text", limit=20000)}

    def describe(self, args):
        return f"Copy “{_short(args['text'], 40)}” to the clipboard"

    async def run(self, args, ctx):
        previous = await _pbpaste()
        await _pbcopy(args["text"])
        return StepResult(evidence=f"Copied {len(args['text'])} characters.", undo={"text": previous})

    async def verify(self, args, result, ctx):
        current = await _pbpaste()
        return Check(current == args["text"], "Clipboard reads back the exact text." if current == args["text"] else "Clipboard text differs.")

    async def undo(self, data, ctx):
        await _pbcopy(data["text"])
        return Check(await _pbpaste() == data["text"], "Previous clipboard text restored.")


class Reply(Action):
    """An instant answer for greetings and questions — changes nothing on the Mac."""

    name = "reply"
    label = "Reply"
    params = (("text", "string", True),)
    summary = "answer the user in the dashboard"
    risk = READ
    produces_text = True
    planner_visible = False
    internal = True

    def normalize(self, args, ctx):
        return {"text": _text(args, "text", limit=1200)}

    def describe(self, args):
        return "Reply to you"

    async def run(self, args, ctx):
        return StepResult(output=args["text"], evidence="Answered on this Mac — nothing was changed.")


class Speak(Action):
    name = "speak"
    label = "Speak"
    params = (("text", "string", True),)
    summary = "say text aloud with the on-device voice"
    risk = ONE_WAY
    aliases = {"message": "text", "content": "text", "input": "text"}

    def normalize(self, args, ctx):
        return {"text": _text(args, "text", limit=4000)}

    def describe(self, args):
        step = re.fullmatch(r"\{\{step(\d+)\}\}", args["text"].strip())
        if step:
            return f"Say the result of step {step.group(1)} aloud"
        return f"Say “{_short(args['text'], 50)}”"

    async def run(self, args, ctx):
        spoken = re.sub(r"^[\-*•]\s*", "", args["text"], flags=re.M)[:900]
        code, _out, err = await run(["say", "-f", "-"], stdin=spoken.encode("utf-8"), timeout=150)
        if code != 0:
            raise MacError(err.strip() or "Speech failed")
        return StepResult(evidence=f"macOS speech synthesiser spoke {len(spoken.split())} words.")

    async def verify(self, args, result, ctx):
        return Check(True, result.evidence)


HEALTH_LABELS = {
    "battery": "Battery", "storage": "Storage", "memory": "Memory", "uptime": "Uptime",
    "macos": "macOS", "cpu_load": "CPU load", "top_memory": "Top memory users",
}


class MacHealth(Action):
    name = "mac_health"
    label = "Mac health report"
    params = ()
    summary = "battery, storage, memory, uptime and top apps"
    risk = READ
    produces_text = True

    def describe(self, args):
        return "Inspect battery, storage, memory and uptime"

    async def run(self, args, ctx):
        stats: dict[str, Any] = {}
        _, batt, _ = await run(["pmset", "-g", "batt"], timeout=10)
        match = re.search(r"(\d+)%;\s*([\w ]+?);", batt)
        if match:
            stats["battery"] = f"{match.group(1)}% ({match.group(2).strip()})"
        elif "AC Power" in batt:
            stats["battery"] = "On AC power"
        usage = shutil.disk_usage(str(HOME))
        stats["storage"] = f"{usage.free / 1e9:.0f} GB free of {usage.total / 1e9:.0f} GB"
        _, mem, _ = await run(["memory_pressure", "-Q"], timeout=10)
        free = re.search(r"free percentage:\s*(\d+)%", mem)
        if free:
            stats["memory"] = f"{free.group(1)}% free"
        _, boot, _ = await run(["sysctl", "-n", "kern.boottime"], timeout=5)
        booted = re.search(r"sec = (\d+)", boot)
        if booted:
            hours = (time.time() - int(booted.group(1))) / 3600
            stats["uptime"] = f"{hours / 24:.1f} days" if hours >= 24 else f"{hours:.1f} hours"
        _, version, _ = await run(["sw_vers", "-productVersion"], timeout=5)
        stats["macos"] = version.strip()
        load = os.getloadavg()[0]
        stats["cpu_load"] = f"{load:.2f} (1-min average)"
        _, ps, _ = await run(["ps", "-axmo", "rss=,comm="], timeout=10)
        top = []
        for row in ps.strip().splitlines()[:4]:
            parts = row.strip().split(None, 1)
            if len(parts) == 2:
                top.append(f"{Path(parts[1]).name} ({int(parts[0]) / 1024:.0f} MB)")
        stats["top_memory"] = ", ".join(top[:3])
        text = "\n".join(f"- {HEALTH_LABELS.get(key, key)}: {value}" for key, value in stats.items())
        return StepResult(output=text, evidence=f"Collected {len(stats)} live readings from macOS.", data=stats)


# ---------------------------------------------------------------------------
# Apps driven through scripting and Accessibility (VS Code, Pages, WhatsApp)
# ---------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"))


def plain_label(text: str) -> str:
    """Accessibility labels carry invisible bidi marks; compare them without."""
    return " ".join(str(text or "").translate(_INVISIBLE).split()).lower()


def _name_core(text: str) -> str:
    """A chat name without surrounding punctuation ("Jaasiir." ≈ "Jaasiir") — letters stay exact."""
    return plain_label(text).strip(" .,!?:;'\"“”‘’()[]-_")


async def _activate(bundle_id: str, *, ready_role: str | None = None) -> None:
    code, _out, err = await run(["open", "-b", bundle_id], timeout=20)
    if code != 0:
        raise MacError(err.strip() or f"Could not open {bundle_id}")
    for _ in range(30):
        try:
            nodes = await ax("dump", bundle_id)
        except AXError:
            nodes = []
        if nodes and (ready_role is None or any(n["role"] == ready_role for n in nodes)):
            return
        await asyncio.sleep(0.3)
    raise MacError("The app opened but its window did not appear. If it is frozen, quit and reopen it, then retry.")


async def _vscode_windows(bundle_id: str) -> list[str]:
    try:
        return [n["label"] for n in await ax("dump", bundle_id, "AXWindow")]
    except AXError:
        return []


class OpenInVSCode(Action):
    name = "open_in_vscode"
    label = "Open in VS Code"
    params = (("path", "string", True),)
    summary = "open a project folder or file in Visual Studio Code"
    risk = ONE_WAY
    app = "Visual Studio Code"
    requires = ("accessibility",)
    aliases = {"folder": "path", "project": "path", "file": "path", "target": "path", "directory": "path"}

    def normalize(self, args, ctx):
        return {"path": str(locate_user_path(_text(args, "path", limit=300), ctx.known_folders, created=ctx.created))}

    def describe(self, args):
        return f"Open {pretty_path(args['path'])} in VS Code"

    async def run(self, args, ctx):
        path = resolve_user_path(args["path"], ctx.known_folders)
        if not path.exists():
            raise MacError(f"{pretty_path(path)} does not exist")
        _name, bundle, bundle_id = find_app("Visual Studio Code")
        code, _out, err = await run(["open", "-a", str(bundle), str(path)], timeout=20)
        if code != 0:
            raise MacError(err.strip() or "VS Code refused to open the folder")
        return StepResult(
            output=str(path),
            evidence=f"Asked VS Code to open {pretty_path(path)}.",
            data={"path": str(path), "bundle_id": bundle_id},
        )

    async def verify(self, args, result, ctx):
        name = Path(result.data["path"]).name.lower()
        for _ in range(16):
            titles = await _vscode_windows(result.data["bundle_id"])
            match = next((t for t in titles if name in t.lower()), None)
            if match:
                return Check(True, f"Accessibility reads a VS Code window titled “{_short(match, 70)}”.")
            await asyncio.sleep(0.5)
        return Check(False, f"No VS Code window shows {name} yet.")


PAGES_BUNDLE = "com.apple.Pages"
PAGES_PING = 'tell application "Pages" to return (count of documents) as text\n'


async def _pages_running() -> bool:
    # LaunchServices can report Pages as running after it quit, so ask Pages itself.
    try:
        await osascript(PAGES_PING, app="Pages", timeout=10)
        return True
    except MacError:
        return False


async def _launch_pages() -> None:
    """AppleScript cannot launch the renamed "Pages Creator Studio" app itself (-600)."""
    if await _pages_running():
        return
    code, _out, err = await run(["open", "-b", PAGES_BUNDLE], timeout=30)
    if code != 0:
        raise MacError(err.strip() or "Pages could not be opened.")
    for _ in range(40):
        await asyncio.sleep(0.5)
        if await _pages_running():
            await asyncio.sleep(0.5)
            return
    raise MacError("Pages did not finish opening.")


async def _require_pages() -> None:
    if not await _pages_running():
        raise MacError("Pages is not open. Open your document in Pages, or ask Human Emulator to create one.")


PAGES_CREATE = """
on run argv
\tset theBody to item 1 of argv
\tset thePath to item 2 of argv
\tset pdfPath to item 3 of argv
\tset AppleScript's text item delimiters to ","
\tset headings to text items of (item 4 of argv)
\tset AppleScript's text item delimiters to ""
\ttell application "Pages"
\t\tactivate
\t\tset doc to make new document
\t\tset body text of doc to theBody
\t\ttell body text of doc
\t\t\tset size of paragraph 1 to 28
\t\t\tset font of paragraph 1 to "Helvetica Neue Bold"
\t\tend tell
\t\trepeat with h in headings
\t\t\tif (h as text) is not "" then
\t\t\t\ttell body text of doc
\t\t\t\t\tset size of paragraph (h as integer) to 18
\t\t\t\t\tset font of paragraph (h as integer) to "Helvetica Neue Bold"
\t\t\t\tend tell
\t\t\tend if
\t\tend repeat
\t\tsave doc in POSIX file thePath
\t\tif pdfPath is not "" then export doc to POSIX file pdfPath as PDF
\t\treturn (id of doc) & linefeed & (name of doc)
\tend tell
end run
"""

PAGES_READ = """
on run argv
\ttell application "Pages"
\t\tif (count of documents) is 0 then return "NONE"
\t\tif (item 1 of argv) is "" then
\t\t\tset doc to front document
\t\telse
\t\t\tset doc to document id (item 1 of argv)
\t\tend if
\t\treturn (id of doc) & linefeed & (name of doc) & linefeed & ((count of characters of body text of doc) as text) & linefeed & (body text of doc)
\tend tell
end run
"""

PAGES_APPEND = """
on run argv
\ttell application "Pages"
\t\tif (count of documents) is 0 then return "NONE"
\t\tactivate
\t\tset doc to front document
\t\tset n to count of characters of body text of doc
\t\tset last paragraph of body text of doc to ((last paragraph of body text of doc) & return & (item 1 of argv))
\t\treturn (id of doc) & linefeed & (name of doc) & linefeed & (n as text)
\tend tell
end run
"""

PAGES_TAIL = """
on run argv
\ttell application "Pages"
\t\tset doc to document id (item 1 of argv)
\t\tset n to (item 2 of argv) as integer
\t\tset total to count of characters of body text of doc
\t\tif total is less than or equal to n then return ""
\t\treturn (characters (n + 1) thru -1 of body text of doc) as text
\tend tell
end run
"""

PAGES_CUT = """
on run argv
\ttell application "Pages"
\t\tset doc to document id (item 1 of argv)
\t\tset n to (item 2 of argv) as integer
\t\tif (count of characters of body text of doc) > n then delete (characters (n + 1) thru -1 of body text of doc)
\t\treturn (count of characters of body text of doc) as text
\tend tell
end run
"""

PAGES_CLOSE = """
on run argv
\ttell application "Pages"
\t\ttry
\t\t\tclose document id (item 1 of argv) saving no
\t\tend try
\tend tell
end run
"""

PAGES_OPEN = """
on run argv
\ttell application "Pages"
\t\tset doc to open POSIX file (item 1 of argv)
\t\treturn id of doc
\tend tell
end run
"""


def pages_body(title: str, content: str) -> tuple[str, list[int]]:
    """Title + content as Pages paragraphs; returns the text and heading paragraph numbers."""
    lines = [title.strip()]
    headings: list[int] = []
    for raw in content.strip().splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            lines.append(stripped.lstrip("#").strip())
            headings.append(len(lines))
        elif re.match(r"^[-*•]\s+", stripped):
            lines.append("• " + re.sub(r"^[-*•]\s+", "", stripped))
        else:
            lines.append(stripped)
    return "\n".join(lines), headings


def _same_text(a: str, b: str) -> bool:
    squash = lambda s: re.sub(r"\s+", "", s.replace("•", ""))  # noqa: E731
    return squash(a) == squash(b)


def _pages_doc(out: str) -> dict[str, Any]:
    if out == "NONE":
        raise MacError("No Pages document is open. Open one, or ask Human Emulator to create one.")
    parts = out.replace("\r", "\n").split("\n", 3)
    doc_id, name, count = (parts + ["", "", "0"])[:3]
    return {"id": doc_id, "name": name, "count": int(count or 0), "text": parts[3] if len(parts) > 3 else ""}


class PagesCreate(Action):
    name = "pages_create"
    label = "Create Pages document"
    params = (("title", "string", True), ("content", "string", True), ("path", "string", False), ("export_pdf", "boolean", False))
    summary = "write a new formatted Pages document (title, # headings, - bullets); optionally export a PDF"
    risk = REVERSIBLE
    app = "Pages"
    aliases = {
        "name": "title", "heading": "title", "text": "content", "body": "content", "contents": "content",
        "file": "path", "location": "path", "pdf": "export_pdf",
    }

    def normalize(self, args, ctx):
        title = _text(args, "title", limit=120)
        raw_path = _text(args, "path", required=False, limit=300) or f"Documents/{_clean_part(title)[:60] or 'Untitled'}.pages"
        path = resolve_user_path(raw_path, ctx.known_folders)
        if path.suffix.lower() != ".pages":
            path = path.with_name(path.stem + ".pages")
        ctx.created.append(path)
        export = args.get("export_pdf")
        return {
            "title": title,
            "content": _text(args, "content", limit=20000),
            "path": str(path),
            "export_pdf": False if export in (None, "") else _bool(args, "export_pdf"),
        }

    def describe(self, args):
        pdf = " + PDF" if args.get("export_pdf") else ""
        return f"Pages document “{_short(args['title'], 40)}” → {pretty_path(args['path'])}{pdf}"

    async def run(self, args, ctx):
        path = _unique(resolve_user_path(args["path"], ctx.known_folders))
        created = _make_parents(path)
        pdf = path.with_suffix(".pdf") if args.get("export_pdf") else None
        if pdf is not None:
            pdf = _unique(pdf)
        body, headings = pages_body(args["title"], args["content"])
        await _launch_pages()
        out = await osascript(
            PAGES_CREATE, body, str(path), str(pdf or ""), ",".join(map(str, headings)), app="Pages", timeout=90
        )
        doc_id, name = (out.split("\n") + [""])[:2]
        files = [str(path)] + ([str(pdf)] if pdf else [])
        return StepResult(
            output=body,
            evidence=f"Pages created “{name}” and saved it to {pretty_path(path)}" + (" with a PDF copy." if pdf else "."),
            undo={"doc": doc_id, "files": files, "folders": created, "path": str(path), "body": body},
            artifact={"kind": "file", "path": str(path)},
            data={"doc": doc_id, "path": str(path), "pdf": str(pdf) if pdf else "", "body": body},
        )

    async def verify(self, args, result, ctx):
        doc = _pages_doc(await osascript(PAGES_READ, result.data["doc"], app="Pages"))
        if not _same_text(doc["text"], result.data["body"]):
            return Check(False, "The text Pages reports differs from what Human Emulator wrote.")
        path = Path(result.data["path"])
        if not path.exists():
            return Check(False, f"{pretty_path(path)} was not saved.")
        detail = ""
        if result.data["pdf"]:
            pdf = Path(result.data["pdf"])
            if not pdf.is_file() or not pdf.read_bytes()[:5] == b"%PDF-":
                return Check(False, "The PDF export is missing or not a PDF.")
            detail = f" and {pretty_path(pdf)} is a valid PDF"
        return Check(True, f"Read back {doc['count']} characters from Pages; {pretty_path(path)} is saved{detail}.")

    async def undo(self, data, ctx):
        path = Path(data.get("path") or data["files"][0])
        doc_id = data["doc"]
        if path.exists() and data.get("body"):
            # Never throw away the user's own edits: read the text back first.
            await _launch_pages()
            try:
                doc = _pages_doc(await osascript(PAGES_READ, doc_id, app="Pages"))
            except MacError:
                doc_id = await osascript(PAGES_OPEN, str(path), app="Pages")
                doc = _pages_doc(await osascript(PAGES_READ, doc_id, app="Pages"))
            if not _same_text(doc["text"], data["body"]):
                return Check(False, f"Kept {pretty_path(path)} because it was edited after Human Emulator created it.")
        if await _pages_running():
            await osascript(PAGES_CLOSE, doc_id, app="Pages")
        moved = [pretty_path(_move_to_undo_bin(Path(f), ctx.mission_id, ctx.step_index)) for f in data["files"] if Path(f).exists()]
        _remove_created_folders(data.get("folders", []))
        return Check(True, f"Closed the document and moved {len(moved)} file(s) to the undo bin.")


class PagesRead(Action):
    name = "pages_read"
    label = "Read Pages document"
    params = ()
    summary = "read the text of the Pages document that is open in front"
    risk = READ
    app = "Pages"
    produces_text = True

    def describe(self, args):
        return "Read the open Pages document"

    async def run(self, args, ctx):
        await _require_pages()
        doc = _pages_doc(await osascript(PAGES_READ, "", app="Pages"))
        return StepResult(
            output=doc["text"][:MAX_TEXT],
            evidence=f"Pages returned {doc['count']} characters from “{doc['name']}”.",
            data={"count": doc["count"]},
        )

    async def verify(self, args, result, ctx):
        if not result.output.strip():
            return Check(False, "The open Pages document is empty.")
        return Check(True, result.evidence)


class PagesAppend(Action):
    name = "pages_append"
    label = "Add to Pages document"
    params = (("text", "string", True),)
    summary = "add paragraphs to the end of the Pages document that is open in front"
    risk = REVERSIBLE
    app = "Pages"
    aliases = {"content": "text", "paragraph": "text", "body": "text", "input": "text"}

    def normalize(self, args, ctx):
        return {"text": _text(args, "text", limit=8000)}

    def describe(self, args):
        return f"Add “{_short(args['text'], 50)}” to the open Pages document"

    async def run(self, args, ctx):
        added = args["text"].strip()
        await _require_pages()
        doc = _pages_doc(await osascript(PAGES_APPEND, added, app="Pages"))
        return StepResult(
            evidence=f"Appended {len(added)} characters to “{doc['name']}”.",
            undo={"doc": doc["id"], "count": doc["count"], "added": added},
            data={"doc": doc["id"], "count": doc["count"], "added": added},
        )

    async def verify(self, args, result, ctx):
        doc = _pages_doc(await osascript(PAGES_READ, result.data["doc"], app="Pages"))
        squash = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
        if not squash(doc["text"]).endswith(squash(result.data["added"])):
            return Check(False, "The document does not end with the added text.")
        return Check(True, f"Read back “{doc['name']}”: it now ends with the added text ({doc['count']} characters).")

    async def undo(self, data, ctx):
        if not await _pages_running():
            return Check(False, "Pages was closed, so the added text could not be checked — it was kept.")
        tail = await osascript(PAGES_TAIL, data["doc"], str(data["count"]), app="Pages")
        if not _same_text(tail, data["added"]):
            return Check(False, "Kept the text because the document was edited after Human Emulator added it.")
        out = await osascript(PAGES_CUT, data["doc"], str(data["count"]), app="Pages")
        return Check(int(out or 0) == data["count"], f"Removed the added text — the document is back to {out} characters.")


WHATSAPP_BUNDLE = "net.whatsapp.WhatsApp"
WHATSAPP_COMPOSER = "Compose message"


def whatsapp_header_matches(nodes: list[dict[str, Any]], contact: str) -> bool:
    """The open chat's header names the contact in its call buttons."""
    want = _name_core(contact)
    expected = {f"start voice call with {want}", f"start video call with {want}", f"open call dropdown menu with {want}"}
    return any(n["role"] == "AXButton" and _name_core(n["label"]) in expected for n in nodes)


def _conversation(nodes: list[dict[str, Any]]) -> list[str]:
    """Plain labels of the open chat's message bubbles (between header and composer)."""
    header = next((n["i"] for n in nodes if n["role"] == "AXButton" and re.match(
        r"^(start (voice|video) call|open call dropdown menu) with ", plain_label(n["label"]))), None)
    composer = next((n["i"] for n in nodes if n["role"] == "AXTextArea"), None)
    if header is None or composer is None:
        return []
    return [plain_label(n["label"]) for n in nodes if header < n["i"] < composer and n["role"] == "AXStaticText"]


_NOT_A_CHAT = re.compile(
    r"(?i)^(start|open|new|more|edit|clear|share|emoji|voice|unread|groups|all|favourites|you|from|search|"
    r"archived|documents|media|links|communities|settings|close|back|cancel|attach|sticker|camera)\b"
)


def whatsapp_chat_names(nodes: list[dict[str, Any]]) -> list[str]:
    """Chat names among WhatsApp's buttons. Rows read "Name, 2 unread messages, …";
    "From Name" is a search filter, not a chat."""
    names: dict[str, None] = {}
    for n in nodes:
        if n["role"] != "AXButton":
            continue
        name = n["label"].translate(_INVISIBLE).split(",")[0].strip()
        if name and len(name) <= 60 and not _NOT_A_CHAT.match(name):
            names.setdefault(name, None)
    return list(names)


def _prefixed(contact: str, names: list[str]) -> list[str]:
    want = _name_core(contact)
    return sorted({n for n in names if _name_core(n).startswith(want + " ")})


def pick_chat(contact: str, names: list[str]) -> str | None:
    """The chat the user meant: an exact name, else the only chat whose name starts with it
    ("Ridhi" → "Ridhi Sharma"). Two candidates means asking, never guessing."""
    want = _name_core(contact)
    if not want:
        return None
    exact = [n for n in names if _name_core(n) == want]
    if exact:
        return exact[0]
    prefixed = _prefixed(contact, names)
    return prefixed[0] if len(prefixed) == 1 else None


def whatsapp_suggestions(nodes: list[dict[str, Any]], contact: str) -> str:
    """Close chat names, offered as a hint — never used to send automatically."""
    squashed = {_squash(name): name for name in whatsapp_chat_names(nodes) if _squash(name)}
    close = difflib.get_close_matches(_squash(contact), list(squashed), n=3, cutoff=0.6)
    return " or ".join(f"“{squashed[c]}”" for c in close)


async def whatsapp_visible_chats() -> list[str]:
    """Chat names WhatsApp shows right now. Never focuses WhatsApp: a closed window is reopened
    in the background (open -g) so names can still be matched."""
    try:
        if not (await permissions()).get("accessibility"):
            return []
        buttons = await ax("dump", WHATSAPP_BUNDLE, "AXButton", timeout=8)
        if not buttons:
            await run(["open", "-g", "-b", WHATSAPP_BUNDLE], timeout=10)
            for _ in range(8):
                await asyncio.sleep(0.25)
                buttons = await ax("dump", WHATSAPP_BUNDLE, "AXButton", timeout=8)
                if buttons:
                    break
        return whatsapp_chat_names(buttons)
    except (MacError, ValueError):
        return []


def whatsapp_sent(nodes: list[dict[str, Any]], message: str) -> tuple[int, str]:
    """How many of *our* bubbles carry exactly this text, and the newest one's status.

    WhatsApp labels outgoing text as "Your message, <text>, <time>, Sent to <chat>, <status>"."""
    prefix = f"your message, {plain_label(message)},"
    mine = [label for label in _conversation(nodes) if label.startswith(prefix)]
    status = mine[-1].rsplit(",", 1)[-1].strip() if mine else ""
    return len(mine), status


def whatsapp_last_mine(nodes: list[dict[str, Any]]) -> str:
    """The newest outgoing bubble's full label ("" if none is on screen)."""
    mine = [label for label in _conversation(nodes) if label.startswith("your message, ")]
    return mine[-1] if mine else ""


async def _composer_text() -> str:
    return str((await ax("read", WHATSAPP_BUNDLE, "AXTextArea", WHATSAPP_COMPOSER)).get("value", ""))


async def _press_chat(contact: str) -> None:
    """Open a chat row: exact label first, then "Name, 2 unread messages, …" style rows.
    Either way the chat header is verified exactly before anything is typed."""
    try:
        await ax("press", WHATSAPP_BUNDLE, "AXButton", contact)
    except AXError as exc:
        if exc.code != 4:
            raise
        await ax("press-name", WHATSAPP_BUNDLE, "AXButton", contact)


class SendWhatsApp(Action):
    name = "send_whatsapp"
    label = "Send WhatsApp message"
    params = (("contact", "string", True), ("message", "string", True))
    summary = "send a WhatsApp message to a chat, named exactly as the user said"
    risk = ONE_WAY
    app = "WhatsApp"
    requires = ("accessibility",)
    aliases = {
        "to": "contact", "name": "contact", "recipient": "contact", "person": "contact", "chat": "contact",
        "text": "message", "body": "message", "content": "message", "msg": "message",
    }

    def normalize(self, args, ctx):
        contact = _text(args, "contact", limit=80).strip("\"'“”")
        # A typed newline would press Return and send early; keep it one line.
        message = " ".join(_text(args, "message", limit=2000).split())
        return {"contact": contact, "message": message}

    def describe(self, args):
        return f"WhatsApp “{_short(args['message'], 40)}” to {args['contact']}"

    async def _search(self, contact: str) -> bool:
        """Type the name into WhatsApp's own search — only if its Search box really has focus."""
        await click_menu("WhatsApp", "File", "Search")
        await asyncio.sleep(0.5)
        nodes = await ax("dump", WHATSAPP_BUNDLE)
        if not any(n["focused"] and _name_core(n["label"]) == "search" for n in nodes):
            return False
        await ax("key", WHATSAPP_BUNDLE, "selectall")
        await ax("key", WHATSAPP_BUNDLE, "delete")
        await ax("type", WHATSAPP_BUNDLE, contact)
        return True

    async def _clear_search(self) -> None:
        try:
            await ax("press", WHATSAPP_BUNDLE, "AXButton", "Clear text")
        except AXError:
            pass

    @staticmethod
    def _not_found(contact: str, nodes: list[dict[str, Any]]) -> str:
        several = _prefixed(contact, whatsapp_chat_names(nodes))
        if len(several) > 1:
            names = " or ".join(f"“{n}”" for n in several[:3])
            return f"More than one WhatsApp chat starts with “{contact}” ({names}), so nothing was sent. Say the full name."
        hint = whatsapp_suggestions(nodes, contact)
        return f"WhatsApp has no chat called “{contact}”, so nothing was sent." + (
            f" Did you mean {hint}? Ask again with that name." if hint else " Use the name exactly as WhatsApp shows it."
        )

    async def _open_chat(self, contact: str) -> tuple[str, list[dict[str, Any]]]:
        """Open the chat and return its real name, confirmed from the chat header."""
        await _activate(WHATSAPP_BUNDLE, ready_role="AXButton")
        nodes = await ax("dump", WHATSAPP_BUNDLE)
        if whatsapp_header_matches(nodes, contact):
            return contact, nodes
        await click_menu("WhatsApp", "View", "Chats")
        await asyncio.sleep(0.5)
        seen = await ax("dump", WHATSAPP_BUNDLE, "AXButton")
        name = pick_chat(contact, whatsapp_chat_names(seen))
        searched = False
        try:
            if name is None:
                # Only rows on screen are visible to Accessibility: search for the rest.
                searched = await self._search(contact)
                for _ in range(16 if searched else 0):
                    await asyncio.sleep(0.3)
                    found = await ax("dump", WHATSAPP_BUNDLE, "AXButton")
                    seen += found
                    name = pick_chat(contact, whatsapp_chat_names(found))
                    if name:
                        break
            if name is None:
                raise MacError(self._not_found(contact, seen))
            await _press_chat(name)
            for _ in range(15):
                await asyncio.sleep(0.3)
                nodes = await ax("dump", WHATSAPP_BUNDLE)
                if whatsapp_header_matches(nodes, name):
                    return name, nodes
            raise MacError(f"Could not confirm that the open chat is “{name}”, so nothing was typed.")
        finally:
            if searched:
                await self._clear_search()

    async def run(self, args, ctx):
        contact, message = args["contact"], args["message"]
        front = await frontmost_app()
        name, nodes = await self._open_chat(contact)
        matched = f" (you said “{contact}”)" if _name_core(name) != _name_core(contact) else ""
        await ctx.emit("observe", f"Chat header confirmed: “{name}”{matched}.")
        if (await _composer_text()).strip():
            raise MacError("This chat already has an unsent draft, so Human Emulator left it alone — nothing was sent.")
        before, _status = whatsapp_sent(nodes, message)
        last_before = whatsapp_last_mine(nodes)
        await ax("focus", WHATSAPP_BUNDLE, "AXTextArea", WHATSAPP_COMPOSER)
        await ax("type", WHATSAPP_BUNDLE, message)
        await asyncio.sleep(0.5)
        typed = await _composer_text()
        still_right = whatsapp_header_matches(await ax("dump", WHATSAPP_BUNDLE), name)
        if plain_label(typed) != plain_label(message) or not still_right:
            await ax("key", WHATSAPP_BUNDLE, "selectall")
            await ax("key", WHATSAPP_BUNDLE, "delete")
            raise MacError("The message box did not hold exactly the approved text in the right chat, so it was cleared — nothing was sent.")
        await ax("key", WHATSAPP_BUNDLE, "return")
        await asyncio.sleep(0.3)
        return StepResult(
            evidence=f"Typed the approved text into “{name}” and pressed send.",
            data={"before": before, "last_before": last_before, "chat": name, "front": front},
        )

    async def verify(self, args, result, ctx):
        chat = result.data.get("chat") or args["contact"]
        prefix = f"your message, {plain_label(args['message'])},"
        try:
            for _ in range(20):
                nodes = await ax("dump", WHATSAPP_BUNDLE)
                count, status = whatsapp_sent(nodes, args["message"])
                last = whatsapp_last_mine(nodes)
                # Older copies of the same text can scroll out of view, so a new newest bubble also counts.
                new_bubble = count > result.data["before"] or (last.startswith(prefix) and last != result.data.get("last_before", ""))
                if new_bubble and not (await _composer_text()).strip():
                    return Check(
                        True,
                        f"Read back from WhatsApp: your new “{_short(args['message'], 40)}” bubble is in the chat "
                        f"“{chat}” (status: {status or 'sending'}) and the message box is empty.",
                    )
                await asyncio.sleep(0.4)
            return Check(False, "Could not see the new message in the chat — check WhatsApp.")
        finally:
            front = result.data.get("front")
            if front and front != WHATSAPP_BUNDLE:
                await asyncio.sleep(1.2)  # let the audience see the sent bubble, then return to the dashboard
                await bring_to_front(front)


# ---------------------------------------------------------------------------
# Any app: the paper's ordered channels (Accessibility tree first, on-device OCR last)
# ---------------------------------------------------------------------------

CALC_BUNDLE = "com.apple.calculator"
CALC_KEYS = {"+": "Add", "-": "Subtract", "*": "Multiply", "/": "Divide", ".": "Point|Decimal"}


def calc_keys(expression: str) -> list[str]:
    """Calculator buttons, by their Accessibility names, for an expression like "128*46"."""
    return ["All Clear|Clear"] + [CALC_KEYS.get(ch, ch) for ch in expression] + ["Equals"]


def display_number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("\u2212", "-").replace("\u202f", "").replace(" ", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.10g}"


class Calculate(Action):
    name = "calculate"
    label = "Calculate in Calculator"
    params = (("expression", "string", True),)
    summary = "work out + − × ÷ in the Calculator app by pressing its buttons by name, then check the display"
    risk = READ
    app = "Calculator"
    produces_text = True
    requires = ("accessibility",)
    planner_visible = False
    aliases = {"expr": "expression", "math": "expression", "sum": "expression", "problem": "expression"}

    def normalize(self, args, ctx):
        expression = intents.parse_calculation("calculate " + _text(args, "expression", limit=120))
        if not expression:
            raise ArgError("expression must be numbers joined by + − × ÷")
        try:
            intents.evaluate(expression)
        except (ValueError, ZeroDivisionError, SyntaxError) as exc:
            raise ArgError(f"cannot work out {expression}: {exc}") from exc
        return {"expression": expression}

    def describe(self, args):
        return f"Work out {intents.pretty_expression(args['expression'])} in Calculator"

    async def run(self, args, ctx):
        expression = args["expression"]
        expected = intents.evaluate(expression)
        keys = calc_keys(expression)
        await _activate(CALC_BUNDLE, ready_role="AXButton")
        await ctx.emit(
            "act",
            f"Pressing {len(keys)} Calculator buttons by name through the Accessibility tree — "
            "no screenshots, no pixel positions, so you can even drag the window while it works.",
        )
        out = await ax("press-seq", CALC_BUNDLE, "350", *keys, timeout=60)
        if not out.get("found"):
            raise MacError(f"Calculator has no button named “{out.get('missing')}”. Switch it to Basic mode and retry.")
        await asyncio.sleep(0.4)
        shown, typed = "", ""
        for node in await ax("dump", CALC_BUNDLE, "AXStaticText"):
            if plain_label(node["label"]) == "edit field":
                shown = node["value"]
            elif plain_label(node["label"]) == "last expression":
                typed = node["value"]
        pretty = intents.pretty_expression(expression)
        return StepResult(
            output=f"{pretty} = {shown or '?'}",
            evidence=(
                f"Accessibility tree: {out['elements']} elements in Calculator → pressed {out['pressed']} buttons "
                f"by name in {out['ms'] / 1000:.1f}s."
            ),
            data={"expected": expected, "shown": shown, "typed": typed},
        )

    async def verify(self, args, result, ctx):
        shown, expected = result.data["shown"], result.data["expected"]
        value = display_number(shown)
        if value is None:
            return Check(False, f"Could not read a number from Calculator's display (it shows “{shown}”).")
        ok = abs(value - expected) <= max(1e-6, abs(expected) * 1e-9)
        typed = f" for “{result.data['typed']}”" if result.data.get("typed") else ""
        return Check(
            ok,
            f"Read back from Calculator's display through Accessibility: {shown}{typed} — "
            + (f"matches the independently computed answer {_format_number(expected)}."
               if ok else f"does NOT match the correct answer {_format_number(expected)}."),
        )


class PressButton(Action):
    name = "press_button"
    label = "Press a button in any app"
    params = (("button", "string", True), ("app", "string", True))
    summary = "press a named button in any Mac app: Accessibility tree first, on-device OCR if it is not there"
    risk = ONE_WAY
    requires = ("accessibility",)
    planner_visible = False
    aliases = {"label": "button", "control": "button", "target": "button", "application": "app"}

    def normalize(self, args, ctx):
        button = _text(args, "button", limit=80).strip("\"'“”")
        app_name, _bundle, bundle_id = find_app(_text(args, "app", limit=80))
        if not bundle_id:
            raise ArgError(f"{app_name} has no bundle identifier")
        return {"button": button, "app": app_name, "bundle_id": bundle_id}

    def describe(self, args):
        return f"Press “{_short(args['button'], 40)}” in {args['app']}"

    async def run(self, args, ctx):
        button, app, bundle_id = args["button"], args["app"], args["bundle_id"]
        await _activate(bundle_id)
        out = await ax("press-label", bundle_id, button)
        if out.get("found"):
            warmed = " (after an 800 ms warm-up walk)" if out.get("warmed") else ""
            return StepResult(
                evidence=(
                    f"Channel 1 · Accessibility tree: {out['elements']} elements{warmed} → pressed "
                    f"{out['role'].removeprefix('AX').lower()} “{out['label']}” by name. No screenshot, no pixel position."
                ),
                data={"channel": "accessibility"},
            )
        await ctx.emit(
            "observe",
            f"Accessibility tree: {out.get('elements', 0)} elements in {app}, none called “{button}” → "
            "falling back to on-device OCR.",
        )
        if not (await permissions()).get("screen"):
            raise MacError(permission_help(["screen"]))
        seen = await ax("vision-click", bundle_id, button, timeout=150)
        if not seen.get("found"):
            read = ", ".join(f"“{line}”" for line in seen.get("seen", [])[:8]) or "nothing"
            raise MacError(
                f"“{button}” is not in {app}'s Accessibility tree, and Apple Vision did not see it on screen "
                f"either (it read {read}). Nothing was clicked."
            )
        return StepResult(
            evidence=(
                f"Channel 2 · On-device OCR: Apple Vision read {seen['lines']} lines in {seen['ms']} ms, matched "
                f"“{seen['match']}” (confidence {seen['confidence']:.2f}) and clicked its centre at "
                f"({seen['x']}, {seen['y']}). The image never left this Mac."
            ),
            data={"channel": "ocr"},
        )

    async def verify(self, args, result, ctx):
        if result.data["channel"] == "accessibility":
            return Check(True, "macOS confirmed the press (the Accessibility press action returned success).")
        return Check(True, "The click was delivered at the text Apple Vision located.")


DESCRIBE_SYSTEM = (
    "You describe a computer screen to someone who cannot see it. You get the app's name and the text read "
    "from its window, top to bottom. In 2-4 short spoken sentences say which app is open, what the main content "
    "is, and one useful thing they could do next. Do not read everything out. Never invent text that is not "
    "there. Plain sentences only: no lists, no markdown."
)


class DescribeScreen(Action):
    name = "describe_screen"
    label = "Describe the screen aloud"
    params = (("app", "string", False),)
    summary = "read the window behind the dashboard (or a named app) on-device and describe it aloud"
    risk = READ
    produces_text = True
    requires = ("accessibility", "screen")
    planner_visible = False

    def normalize(self, args, ctx):
        wanted = str(args.get("app") or "").strip()
        if not wanted:
            return {"app": "", "bundle_id": ""}
        app_name, _bundle, bundle_id = find_app(wanted)
        return {"app": app_name, "bundle_id": bundle_id}

    def describe(self, args):
        return f"Describe {args['app'] or 'the window behind this dashboard'} aloud"

    async def run(self, args, ctx):
        front = await frontmost_app()
        bundle_id, app = args["bundle_id"], args["app"]
        if not bundle_id:
            behind = await ax("under", front or "-")
            bundle_id, app = behind.get("bundle_id", ""), behind.get("name", "")
            if not bundle_id:
                raise MacError(
                    "No other window is visible on this desktop (full-screen apps live on their own desktop). "
                    "Name the app instead, e.g. “describe my WhatsApp screen”."
                )
        await ctx.emit("observe", f"Reading {app}'s window with Apple Vision on this Mac…")
        try:
            seen = await ax("vision-read", bundle_id, timeout=150)
        finally:
            if front and front != bundle_id:
                await bring_to_front(front)
        lines = [line for line in seen.get("lines", []) if line.strip()]
        if not lines:
            raise MacError(f"Apple Vision found no text in {app}'s window.")
        result = await llm.chat(
            [
                {"role": "system", "content": DESCRIBE_SYSTEM},
                {"role": "user", "content": f"App: {app}\nText in its window:\n" + "\n".join(lines)[:3000]},
            ],
            on_delta=ctx.on_delta, num_predict=170, temperature=0.2,
        )
        summary = " ".join(re.sub(r"[*#`]", "", result.text).split())
        if not summary:
            raise MacError("The on-device model returned no description.")
        await ctx.emit("act", "Speaking the description aloud…")
        code, _out, err = await run(["say", "-f", "-"], stdin=summary.encode("utf-8"), timeout=120)
        if code != 0:
            raise MacError(err.strip() or "Speech failed")
        return StepResult(
            output=summary,
            evidence=(
                f"Apple Vision read {len(lines)} lines from {app}'s window in {seen.get('ms', 0)} ms; the on-device "
                "model summarised them and macOS spoke the summary. The image never left this Mac."
            ),
            data={"lines": len(lines), "app": app},
        )


CATALOG: dict[str, Action] = {
    action.name: action
    for action in [
        CreateFolder(),
        WriteFile(),
        CreateNote(),
        CreateReminder(),
        CreateEvent(),
        ReadAgenda(),
        ReadScreen(),
        Compose(),
        BuildWebpage(),
        OpenApp(),
        OpenPath(),
        SetVolume(),
        SetDarkMode(),
        CopyToClipboard(),
        Speak(),
        MacHealth(),
        OpenInVSCode(),
        PagesCreate(),
        PagesRead(),
        PagesAppend(),
        SendWhatsApp(),
        Calculate(),
        PressButton(),
        DescribeScreen(),
        Reply(),
    ]
}
