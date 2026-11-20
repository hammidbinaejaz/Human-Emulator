"""Injection-safe wrappers around macOS tools (osascript, screencapture, OCR).

AppleScript templates are static. User-supplied text is only ever passed as
`argv` items to `on run argv`, never spliced into script source.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
STATE_DIR = Path(os.environ.get("CASE_MC_HOME", HOME / ".case-mission")).expanduser()
BIN_DIR = STATE_DIR / "bin"
CAPTURE_DIR = STATE_DIR / "captures"
UNDO_DIR = STATE_DIR / "undo-bin"
OCR_SOURCE = Path(__file__).resolve().parent / "native" / "case_ocr.swift"
OCR_BINARY = BIN_DIR / "case-ocr"
AX_SOURCE = Path(__file__).resolve().parent / "native" / "case_ax.swift"
AX_BINARY = BIN_DIR / "case-ax"
LISTEN_SOURCE = Path(__file__).resolve().parent / "native" / "case_listen.swift"
LISTEN_BINARY = BIN_DIR / "case-listen"
VOICE_DIR = STATE_DIR / "voice"


class MacError(RuntimeError):
    """A macOS operation failed; the message is safe to show to the user."""


def ensure_dirs() -> None:
    for path in (STATE_DIR, BIN_DIR, CAPTURE_DIR, UNDO_DIR, VOICE_DIR):
        path.mkdir(parents=True, exist_ok=True)


async def run(
    cmd: list[str], *, stdin: bytes | None = None, timeout: float = 30
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise MacError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s.") from exc
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def friendly_applescript_error(stderr: str, app: str | None = None) -> str:
    text = stderr.strip()
    target = app or "the app"
    if "-1743" in text or "not authorized" in text.lower() or "not allowed" in text.lower():
        return (
            f"macOS blocked Human Emulator from controlling {target}. Allow it in System Settings → "
            "Privacy & Security → Automation (for the Terminal / editor running Human Emulator), then retry."
        )
    if "-1712" in text:
        return f"{target} did not respond. Quit and reopen {target}, then retry."
    if "-1728" in text or "can’t get" in text.lower() or "can't get" in text.lower():
        return f"{target} could not find the requested item."
    if "-600" in text:
        return f"{target} is not running and could not be launched."
    return text.splitlines()[-1] if text else "AppleScript failed."


async def osascript(script: str, *args: str, app: str | None = None, timeout: float = 60) -> str:
    try:
        code, out, err = await run(
            ["osascript", "-", *[str(a) for a in args]], stdin=script.encode("utf-8"), timeout=timeout
        )
    except MacError as exc:
        target = app or "The app"
        raise MacError(
            f"{target} did not respond within {timeout:.0f}s. If a macOS permission prompt is showing, "
            f"click Allow; otherwise quit and reopen {target} and retry."
        ) from exc
    if code != 0:
        raise MacError(friendly_applescript_error(err, app))
    return out.strip()


# AppleScript date construction that is independent of the user's locale.
DATE_HANDLER = """
on mkdate(y, mo, d, h, mi)
\tset t to current date
\tset day of t to 1
\tset year of t to (y as integer)
\tset month of t to (mo as integer)
\tset day of t to (d as integer)
\tset time of t to ((h as integer) * hours + (mi as integer) * minutes)
\treturn t
end mkdate
"""


def date_args(value: datetime) -> list[str]:
    return [str(value.year), str(value.month), str(value.day), str(value.hour), str(value.minute)]


_build_locks: dict[str, asyncio.Lock] = {}


async def ensure_binary(source: Path, binary: Path, what: str) -> Path:
    """Compile a native Swift helper once (requires the Xcode command line tools)."""
    ensure_dirs()
    lock = _build_locks.setdefault(str(binary), asyncio.Lock())
    async with lock:
        if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
            return binary
        code, _out, err = await run(["swiftc", "-O", str(source), "-o", str(binary)], timeout=300)
        if code != 0:
            raise MacError(f"Could not build the on-device {what} helper: {err.strip()[-300:]}")
    return binary


async def ensure_ocr_binary() -> Path:
    return await ensure_binary(OCR_SOURCE, OCR_BINARY, "OCR")


async def ax(*args: str, timeout: float = 20) -> Any:
    """Run the Accessibility helper and return its JSON output."""
    binary = await ensure_binary(AX_SOURCE, AX_BINARY, "Accessibility")
    code, out, err = await run([str(binary), *args], timeout=timeout)
    if code == 2:
        raise MacError(permission_help(["accessibility"]))
    if code != 0:
        raise AXError(err.strip() or f"Accessibility helper failed ({code})", code)
    return json.loads(out) if out.strip() else None


class AXError(MacError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Permissions. macOS grants them to the app that *launched* Human Emulator
# (Terminal, iTerm, VS Code…), so every message names that app.
# ---------------------------------------------------------------------------

PERMISSION_NAMES = {"accessibility": "Accessibility", "screen": "Screen Recording"}
PRIVACY_PANES = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}
_host_cache: str | None = None
_permission_cache: tuple[float, dict[str, bool]] = (0.0, {})


def host_app() -> str:
    """Name of the app that launched this server, e.g. "Terminal" or "Visual Studio Code"."""
    global _host_cache
    if _host_cache is not None:
        return _host_cache
    import re
    import subprocess

    pid, name = os.getppid(), ""
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not out:
            break
        ppid, _, comm = out.partition(" ")
        match = re.search(r"/([^/]+)\.app/", comm)
        if match:
            name = match.group(1)
            break
        if not ppid.strip().isdigit() or int(ppid) <= 1:
            break
        pid = int(ppid)
    _host_cache = name or "Terminal"
    return _host_cache


async def _helper(*args: str) -> dict[str, Any]:
    binary = await ensure_binary(AX_SOURCE, AX_BINARY, "Accessibility")
    code, out, err = await run([str(binary), *args], timeout=15)
    if code != 0:
        raise MacError(err.strip() or "permission check failed")
    return json.loads(out)


async def permissions(*, fresh: bool = False) -> dict[str, bool]:
    """Which macOS permissions the host app has (cached briefly for status polling)."""
    global _permission_cache
    checked_at, cached = _permission_cache
    if not fresh and cached and time.monotonic() - checked_at < 3:
        return cached
    try:
        ax_state, screen_state = await asyncio.gather(_helper("trusted"), _helper("screen"))
        result = {"accessibility": bool(ax_state.get("trusted")), "screen": bool(screen_state.get("granted"))}
    except (MacError, ValueError):
        result = {"accessibility": False, "screen": False}
    _permission_cache = (time.monotonic(), result)
    return result


def permission_help(missing: list[str]) -> str:
    host = host_app()
    names = " and ".join(PERMISSION_NAMES.get(m, m) for m in missing)
    text = (
        f"macOS has not allowed {host} (which runs Human Emulator) to use {names}. "
        f"Open System Settings → Privacy & Security → {names}, switch on {host}"
    )
    if "screen" in missing:
        return text + f", then quit and reopen {host} and type: hi human emulator"
    return text + ", then press Engage again."


async def open_permission_settings(which: str) -> None:
    """Show macOS's own prompt (adds the host app to the list) and open the right Settings pane."""
    if which not in PRIVACY_PANES:
        raise MacError(f"Unknown permission {which!r}")
    global _permission_cache
    _permission_cache = (0.0, {})
    try:
        await _helper("trusted" if which == "accessibility" else "screen", "--prompt")
    except MacError:
        pass
    await run(["open", PRIVACY_PANES[which]], timeout=10)


async def frontmost_app() -> str:
    try:
        return str((await _helper("front")).get("bundle_id") or "")
    except (MacError, ValueError):
        return ""


async def bring_to_front(bundle_id: str) -> None:
    if bundle_id:
        await run(["open", "-b", bundle_id], timeout=10)


MENU_CLICK = """
on run argv
\ttell application "System Events"
\t\ttell process (item 1 of argv)
\t\t\tset frontmost to true
\t\t\tset theMenu to first menu bar item of menu bar 1 whose name ends with (item 2 of argv)
\t\t\tclick (first menu item of menu 1 of theMenu whose name ends with (item 3 of argv))
\t\tend tell
\tend tell
end run
"""


async def click_menu(process: str, menu: str, item: str) -> None:
    await osascript(MENU_CLICK, process, menu, item, app=process, timeout=20)


async def capture_screen(name: str) -> Path:
    ensure_dirs()
    target = CAPTURE_DIR / f"{name}.png"
    code, _out, err = await run(["screencapture", "-x", "-m", str(target)], timeout=20)
    if code != 0 or not target.exists():
        raise MacError(permission_help(["screen"]) + f" ({err.strip()})")
    _prune_captures(keep=30)
    return target


def _prune_captures(keep: int) -> None:
    captures = sorted(CAPTURE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in captures[keep:]:
        stale.unlink(missing_ok=True)


def delete_captures(mission_id: str) -> int:
    """Screenshots can show private things; they go with the mission they belong to."""
    removed = 0
    for capture in CAPTURE_DIR.glob(f"{mission_id}-*.png"):
        capture.unlink(missing_ok=True)
        removed += 1
    return removed


async def recognize_text(image: Path) -> dict:
    binary = await ensure_ocr_binary()
    code, out, err = await run([str(binary), str(image)], timeout=150)
    if code != 0:
        raise MacError(f"On-device OCR failed: {err.strip()}")
    return json.loads(out)


async def warm_up_ocr() -> str:
    """Load Apple Vision's text models once so the first screen read is fast."""
    binary = await ensure_ocr_binary()
    code, out, err = await run([str(binary), "--warmup"], timeout=240)
    if code != 0:
        raise MacError(f"OCR warm-up failed: {err.strip()}")
    # The Accessibility helper reads windows with a different Vision setup; warm that too.
    helper = await ensure_binary(AX_SOURCE, AX_BINARY, "Accessibility")
    await run([str(helper), "vision-warmup"], timeout=240)
    return out.strip()


_network_cache: tuple[float, bool] = (0.0, False)


def _probe_network() -> bool:
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=0.7):
                return True
        except OSError:
            continue
    return False


async def network_online() -> bool:
    """Cheap, cached reachability probe used only for the status badge."""
    global _network_cache
    checked_at, online = _network_cache
    if time.monotonic() - checked_at < 4:
        return online
    online = await asyncio.to_thread(_probe_network)
    _network_cache = (time.monotonic(), online)
    return online
