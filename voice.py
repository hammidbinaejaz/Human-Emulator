"""Offline voice: on-device speech-to-text (Apple SpeechAnalyzer) and spoken replies (`say`)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

from macos import LISTEN_BINARY, LISTEN_SOURCE, VOICE_DIR, MacError, ensure_binary, ensure_dirs, run

MAX_AUDIO_BYTES = 4 * 1024 * 1024
PREFERRED_VOICES = [os.environ.get("CASE_VOICE", ""), "Daniel", "Samantha"]

# The recogniser does not know app names; fix the ones Human Emulator cares about.
FIXUPS = [
    (r"\b(versus|vs\.?|v\.?\s?s\.?|ves)\s*code\b", "VS Code"),
    (r"\b(what'?s\s?app|whats\s?up|what sap)\b", "WhatsApp"),
    (r"\bhuman emulators?\b", "Human Emulator"),
    (r"\bkey ?note\b", "Keynote"),
    # "say hi" is often heard as "say high".
    (r"\b(say|send|text|message|whatsapp)\s+high\b", r"\1 hi"),
]

_speaker: asyncio.subprocess.Process | None = None
_voice: str | None = None


def clean_transcript(text: str) -> str:
    text = " ".join((text or "").split())
    for pattern, replacement in FIXUPS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


async def prepare() -> None:
    binary = await ensure_binary(LISTEN_SOURCE, LISTEN_BINARY, "speech")
    code, _out, err = await run([str(binary), "--prepare"], timeout=600)
    if code != 0:
        raise MacError(f"On-device speech is unavailable: {err.strip()}")


async def transcribe(audio: bytes) -> dict:
    if not audio:
        raise MacError("No audio was recorded.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise MacError("That recording is too long — keep commands under a minute.")
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise MacError("Audio must be a WAV recording.")
    ensure_dirs()
    binary = await ensure_binary(LISTEN_SOURCE, LISTEN_BINARY, "speech")
    path = VOICE_DIR / f"{uuid.uuid4().hex}.wav"
    path.write_bytes(audio)
    try:
        code, out, err = await run([str(binary), str(path)], timeout=60)
    finally:
        path.unlink(missing_ok=True)
    if code != 0:
        raise MacError(f"On-device speech recognition failed: {err.strip()[-200:]}")
    result = json.loads(out)
    result["raw"] = result.get("text", "")
    result["text"] = clean_transcript(result.get("text", ""))
    return result


async def _pick_voice() -> str:
    global _voice
    if _voice is None:
        _code, out, _err = await run(["say", "-v", "?"], timeout=10)
        installed = {re.split(r"\s{2,}| \(", line.strip())[0] for line in out.splitlines() if line.strip()}
        _voice = next((v for v in PREFERRED_VOICES if v and v in installed), "")
    return _voice


async def stop_speaking() -> None:
    global _speaker
    if _speaker is not None and _speaker.returncode is None:
        _speaker.terminate()
        await _speaker.wait()
    _speaker = None


async def say(text: str, *, wait: bool = False) -> None:
    """Speak a reply on this Mac, interrupting any reply still playing."""
    global _speaker
    text = " ".join((text or "").split())[:600]
    if not text:
        return
    await stop_speaking()
    voice = await _pick_voice()
    cmd = ["say", *(["-v", voice] if voice else []), "-r", "190", "-f", "-"]
    _speaker = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    assert _speaker.stdin is not None
    _speaker.stdin.write(text.encode("utf-8"))
    await _speaker.stdin.drain()
    _speaker.stdin.close()
    if wait:
        try:
            await asyncio.wait_for(_speaker.wait(), timeout=60)
        except asyncio.TimeoutError:
            await stop_speaking()
