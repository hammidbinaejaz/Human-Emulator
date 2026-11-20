"""Client for the on-device model served by Ollama on the loopback interface."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

OLLAMA_URL = os.environ.get("CASE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("CASE_MODEL", "qwen3.5:2b-q4_K_M")
KEEP_ALIVE = os.environ.get("CASE_KEEP_ALIVE", "30m")
NUM_CTX = int(os.environ.get("CASE_NUM_CTX", "4096"))

DeltaCallback = Callable[[str], Awaitable[None]]

_capabilities: dict[str, set[str]] = {}


async def capabilities(model: str | None = None) -> set[str]:
    """What the installed model supports (e.g. "thinking", "vision"), cached per model."""
    model = model or MODEL
    if model not in _capabilities:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{OLLAMA_URL}/api/show", json={"model": model})
            _capabilities[model] = set(response.json().get("capabilities") or []) if response.status_code == 200 else set()
        except (httpx.HTTPError, ValueError):
            return set()
    return _capabilities[model]


class LLMError(RuntimeError):
    """The local model could not be reached or returned an unusable answer."""


@dataclass
class LLMResult:
    text: str
    tokens: int
    tokens_per_s: float
    duration_ms: int
    prompt_tokens: int = 0
    prompt_ms: int = 0
    load_ms: int = 0


def endpoint_is_local() -> bool:
    host = urlparse(OLLAMA_URL).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1"}


async def chat(
    messages: list[dict[str, str]],
    *,
    schema: dict[str, Any] | None = None,
    on_delta: DeltaCallback | None = None,
    num_predict: int = 700,
    temperature: float = 0.0,
) -> LLMResult:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    if schema is not None:
        body["format"] = schema
    # Reasoning models would "think" for hundreds of hidden tokens first; plans are
    # schema-constrained JSON, so answer directly.
    if "thinking" in await capabilities(MODEL):
        body["think"] = False

    started = time.monotonic()
    chunks: list[str] = []
    final: dict[str, Any] = {}
    timeout = httpx.Timeout(connect=3.0, read=180.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=body) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")
                    raise LLMError(f"Local model error ({response.status_code}): {detail[:300]}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise LLMError(f"Local model error: {event['error']}")
                    delta = (event.get("message") or {}).get("content") or ""
                    if delta:
                        chunks.append(delta)
                        if on_delta is not None:
                            await on_delta(delta)
                    if event.get("done"):
                        final = event
    except httpx.ConnectError as exc:
        raise LLMError(
            f"The on-device model is not running at {OLLAMA_URL}. Start it with `ollama serve`."
        ) from exc
    except httpx.TimeoutException as exc:
        raise LLMError("The on-device model took too long to respond.") from exc

    eval_count = int(final.get("eval_count") or len(chunks))
    eval_ns = int(final.get("eval_duration") or 0)
    return LLMResult(
        text="".join(chunks),
        tokens=eval_count,
        tokens_per_s=round(eval_count / (eval_ns / 1e9), 1) if eval_ns else 0.0,
        duration_ms=int((time.monotonic() - started) * 1000),
        prompt_tokens=int(final.get("prompt_eval_count") or 0),
        prompt_ms=int(int(final.get("prompt_eval_duration") or 0) / 1e6),
        load_ms=int(int(final.get("load_duration") or 0) / 1e6),
    )


async def engine_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "url": OLLAMA_URL,
        "local": endpoint_is_local(),
        "model": MODEL,
        "up": False,
        "installed": False,
        "loaded": False,
        "size_gb": None,
        "version": None,
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            version = await client.get(f"{OLLAMA_URL}/api/version")
            tags = await client.get(f"{OLLAMA_URL}/api/tags")
            loaded = await client.get(f"{OLLAMA_URL}/api/ps")
    except httpx.HTTPError:
        return status
    status["up"] = True
    status["version"] = version.json().get("version")
    for model in tags.json().get("models", []):
        if model.get("name") == MODEL:
            status["installed"] = True
            status["size_gb"] = round(model.get("size", 0) / 1e9, 1)
            details = model.get("details") or {}
            status["parameters"] = details.get("parameter_size")
            status["quantization"] = details.get("quantization_level")
    status["loaded"] = any(m.get("name") == MODEL for m in loaded.json().get("models", []))
    return status


async def warm_up() -> None:
    """Load the model into memory so the first mission does not pay the load cost."""
    timeout = httpx.Timeout(connect=3.0, read=240.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await client.post(
            f"{OLLAMA_URL}/api/generate", json={"model": MODEL, "keep_alive": KEEP_ALIVE}
        )
