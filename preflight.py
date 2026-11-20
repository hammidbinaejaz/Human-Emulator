"""Startup checklist printed by run.sh: what macOS must allow before a demo."""

from __future__ import annotations

import asyncio
from pathlib import Path

from macos import MacError, ensure_dirs, host_app, open_permission_settings, permissions


async def main() -> None:
    ensure_dirs()
    host = host_app()
    try:
        granted = await permissions(fresh=True)
    except MacError as exc:
        print(f"  ! Could not check permissions: {exc}")
        return
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    print(f"  {mark(granted['accessibility'])} Accessibility for {host}  — WhatsApp, VS Code and app control")
    print(f"  {mark(granted['screen'])} Screen Recording for {host}  — “read my screen”")
    print(f"  {mark(Path('/Applications/WhatsApp.app').exists())} WhatsApp installed")
    if not granted["accessibility"]:
        print(f"  → Opening System Settings: switch ON “{host}” under Accessibility (takes effect immediately).")
        await open_permission_settings("accessibility")
    if not granted["screen"]:
        print(f"  → For screen reading: System Settings → Privacy & Security → Screen Recording → “{host}”, then reopen {host}.")


if __name__ == "__main__":
    asyncio.run(main())
