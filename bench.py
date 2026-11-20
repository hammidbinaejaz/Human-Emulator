"""Benchmark on-device planner models on realistic missions.

Usage:  .venv/bin/python bench.py qwen2.5:3b qwen3.5:2b qwen3.5:4b

For each model: unload the others, warm it up, then plan every goal below and
check the validated plan (right actions, key arguments, no validator errors).
Nothing is executed — planning only.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from datetime import datetime

import httpx

import llm
import planner

GOALS: list[tuple[str, set[str], dict]] = [
    (
        "Set up my viva prep: create a Viva Prep folder on the Desktop with a checklist of 5 things to rehearse, "
        "and remind me tomorrow at 9am to rehearse",
        {"create_folder", "write_file", "create_reminder"},
        {},
    ),
    (
        "Deep work mode: switch to dark mode, set volume to 15, and remind me in 25 minutes to take a break",
        {"set_dark_mode", "set_volume", "create_reminder"},
        {"set_volume": {"level": 15}, "set_dark_mode": {"enabled": True}},
    ),
    ("What's on my calendar today? Tell me out loud", {"read_agenda", "speak"}, {}),
    ("Open my final year project folder in VS Code", {"open_in_vscode"}, {}),
    ("Send hi to Mom on WhatsApp", {"send_whatsapp"}, {"send_whatsapp": {"contact": "Mom"}}),
    (
        "Write a short essay about renewable energy in Pages and export it as a PDF",
        {"pages_create"},
        {"pages_create": {"export_pdf": True}},
    ),
    (
        "Add a closing line saying 'Thanks for reading' to the Pages document I have open",
        {"pages_append"},
        {},
    ),
    ("How is my Mac doing? Tell me out loud", {"mac_health", "speak"}, {}),
    ("Read my screen and save the key points as a note", {"read_screen", "create_note"}, {}),
    (
        "Order a pizza and block 30 minutes tomorrow at 2pm for lunch",
        {"create_event"},
        {"create_event": {"duration_minutes": 30}},
    ),
]


async def unload_all(keep: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        loaded = (await client.get(f"{llm.OLLAMA_URL}/api/ps")).json().get("models", [])
        for model in loaded:
            if model["name"] != keep:
                await client.post(f"{llm.OLLAMA_URL}/api/generate", json={"model": model["name"], "keep_alive": 0})


async def resident_gb(model: str) -> float:
    async with httpx.AsyncClient(timeout=10) as client:
        for entry in (await client.get(f"{llm.OLLAMA_URL}/api/ps")).json().get("models", []):
            if entry["name"] == model:
                return round(entry.get("size", 0) / 1e9, 2)
    return 0.0


def score(plan: planner.Plan, expected: set[str], arg_checks: dict, goal: str) -> tuple[bool, str]:
    actions = [s.action for s in plan.steps]
    missing = expected - set(actions)
    if missing:
        return False, f"missing {sorted(missing)}"
    for step in plan.steps:
        for key, want in arg_checks.get(step.action, {}).items():
            if step.args.get(key) != want:
                return False, f"{step.action}.{key}={step.args.get(key)!r}"
    if "pizza" in goal and not plan.unsupported:
        return False, "did not flag pizza as unsupported"
    if plan.errors:
        return False, f"validator: {plan.errors[0]}"
    return True, "ok"


async def bench(model: str) -> dict:
    llm.MODEL = model
    await unload_all(keep=model)
    started = time.monotonic()
    await llm.warm_up()
    load_s = time.monotonic() - started
    rows = []
    for goal, expected, checks in GOALS:
        t0 = time.monotonic()
        try:
            plan, runs = await planner.make_plan(goal, datetime.now())
            ok, why = score(plan, expected, checks, goal)
            tokens = sum(r.tokens for r in runs)
            rate = runs[-1].tokens_per_s
            prompt_ms = sum(r.prompt_ms for r in runs)
            steps = [s.action for s in plan.steps]
        except Exception as exc:  # noqa: BLE001
            ok, why, tokens, rate, prompt_ms, steps = False, f"error: {exc}", 0, 0.0, 0, []
        rows.append({
            "goal": goal[:60], "ok": ok, "why": why, "ms": int((time.monotonic() - t0) * 1000),
            "tokens": tokens, "tok_s": rate, "prompt_ms": prompt_ms, "steps": steps,
        })
        print(f"  {'✓' if ok else '✗'} {int((time.monotonic() - t0) * 1000):>6} ms  {goal[:58]:<58} {why if not ok else ''}", flush=True)
    times = [r["ms"] for r in rows]
    return {
        "model": model,
        "passed": sum(r["ok"] for r in rows),
        "total": len(rows),
        "median_ms": int(statistics.median(times)),
        "mean_ms": int(statistics.mean(times)),
        "tok_s": round(statistics.mean([r["tok_s"] for r in rows if r["tok_s"]] or [0]), 1),
        "load_s": round(load_s, 1),
        "resident_gb": await resident_gb(model),
        "rows": rows,
    }


async def main(models: list[str]) -> None:
    results = []
    for model in models:
        print(f"\n== {model}", flush=True)
        results.append(await bench(model))
    print("\n| Model | Correct plans | Median plan time | Mean plan time | Generation | Memory |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['model']} | {r['passed']}/{r['total']} | {r['median_ms'] / 1000:.1f}s | {r['mean_ms'] / 1000:.1f}s | {r['tok_s']} tok/s | {r['resident_gb']} GB |")
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main([a for a in sys.argv[1:] if not a.startswith("--")] or [llm.MODEL]))
