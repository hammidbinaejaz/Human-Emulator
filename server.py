"""Human Emulator Mission Control — local web server.

Run:  python -m uvicorn server:app --host 127.0.0.1 --port 8765
Binds to loopback only; the dashboard is served from ./static.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import llm
import voice
from actions import CATALOG
from engine import Engine, EngineBusy, InvalidState, MissionNotFound
from macos import CAPTURE_DIR, MacError, ensure_dirs, host_app, network_online, open_permission_settings, permissions, warm_up_ocr
from store import MissionStore, RoutineStore

log = logging.getLogger("case.mission_control")
STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("CASE_PORT", "8765"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CLIENT_HEADER = "x-case-client"
SCHEDULER_SECONDS = 15
# Lets the dashboard tell a restarted server apart from the old one.
STARTED_AT = time.time()

engine: Engine | None = None
_warmup = {"model": "pending", "ocr": "pending", "speech": "pending"}


def _engine() -> Engine:
    assert engine is not None
    return engine


async def _prepare() -> None:
    try:
        _warmup["ocr"] = "warming"
        await warm_up_ocr()
        _warmup["ocr"] = "ready"
    except Exception as exc:  # noqa: BLE001
        _warmup["ocr"] = f"unavailable: {exc}"
    try:
        _warmup["model"] = "loading"
        await llm.warm_up()
        _warmup["model"] = "ready"
    except Exception as exc:  # noqa: BLE001
        _warmup["model"] = f"unavailable: {exc}"
    try:
        _warmup["speech"] = "preparing"
        await voice.prepare()
        _warmup["speech"] = "ready"
    except Exception as exc:  # noqa: BLE001
        _warmup["speech"] = f"unavailable: {exc}"


async def _scheduler() -> None:
    while True:
        try:
            await _engine().tick_routines()
        except Exception:  # noqa: BLE001 - the scheduler must never die
            log.exception("routine scheduler tick failed")
        await asyncio.sleep(SCHEDULER_SECONDS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engine
    ensure_dirs()
    engine = Engine(MissionStore(), routines=RoutineStore())
    prepare = asyncio.create_task(_prepare())
    scheduler = asyncio.create_task(_scheduler())
    yield
    prepare.cancel()
    scheduler.cancel()


app = FastAPI(title="Human Emulator Mission Control", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Block DNS-rebinding and cross-site requests against this loopback server."""
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Human Emulator only answers on 127.0.0.1."}, status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get(CLIENT_HEADER) != "dashboard" or (
            origin and origin.split("://", 1)[-1] not in ALLOWED_HOSTS
        ):
            return JSONResponse({"detail": "Requests must come from the Human Emulator dashboard."}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class MissionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)


class RoutineRequest(BaseModel):
    when: str = Field(min_length=3, max_length=120)


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1200)
    wait: bool = False


def _hardware() -> dict[str, str]:
    def sysctl(name: str) -> str:
        try:
            return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    memory = sysctl("hw.memsize")
    return {
        "chip": sysctl("machdep.cpu.brand_string") or platform.machine(),
        "memory_gb": f"{int(memory) / 1073741824:.0f}" if memory.isdigit() else "?",
        "macos": platform.mac_ver()[0],
    }


HARDWARE = _hardware()


@app.get("/api/status")
async def status():
    model, online, granted = await asyncio.gather(llm.engine_status(), network_online(), permissions())
    current = _engine()
    return {
        "model": model,
        "warmup": _warmup,
        "ocr_ready": _warmup["ocr"] == "ready",
        "network": "online" if online else "offline",
        "cloud_services": [],
        "busy": current.active_id is not None,
        "active_mission": current.active_id,
        "hardware": HARDWARE,
        "speech_ready": _warmup["speech"] == "ready",
        "started_at": STARTED_AT,
        "permissions": granted,
        "host_app": host_app(),
    }


@app.post("/api/permissions/{which}/open")
async def fix_permission(which: str):
    try:
        await open_permission_settings(which)
    except MacError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"opened": which, "host_app": host_app()}


@app.get("/api/skills")
async def skills():
    return {"skills": [action.catalog_entry() for action in CATALOG.values() if not action.internal]}


@app.get("/api/missions")
async def list_missions(limit: int = 30):
    return {"missions": _engine().recent(max(1, min(limit, 100)))}


@app.get("/api/missions/{mission_id}")
async def get_mission(mission_id: str):
    try:
        return _engine().public(_engine().get(mission_id))
    except MissionNotFound as exc:
        raise HTTPException(status_code=404, detail="Mission not found") from exc


@app.post("/api/missions")
async def create_mission(request: MissionRequest):
    try:
        return _engine().create(request.goal)
    except EngineBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/missions/{mission_id}/routine")
async def save_mission_as_routine(mission_id: str, request: RoutineRequest):
    try:
        return _engine().save_as_routine(mission_id, request.when)
    except MissionNotFound as exc:
        raise HTTPException(status_code=404, detail="Mission not found") from exc
    except (InvalidState, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/history/clear")
async def clear_history():
    return _engine().clear()


@app.post("/api/restart")
async def restart():
    """Restart this server in place (same process, terminal and settings)."""
    if _engine().active_id is not None:
        raise HTTPException(status_code=409, detail="Human Emulator is busy — stop the mission or wait for it to finish, then restart.")
    asyncio.get_running_loop().call_later(0.4, _restart_process)
    return {"restarting": True, "started_at": STARTED_AT}


def _restart_process() -> None:
    log.warning("Restarting Human Emulator…")
    argv = list(getattr(sys, "orig_argv", []))[1:] or ["-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(PORT)]
    os.execv(sys.executable, [sys.executable, *argv])


# Registered after /routine: Starlette uses the first matching route.
@app.post("/api/missions/{mission_id}/{command}")
async def mission_command(mission_id: str, command: str):
    handlers = {
        "approve": _engine().approve,
        "cancel": _engine().cancel,
        "abort": _engine().abort,
        "undo": _engine().undo,
        "delete": _engine().delete,
    }
    handler = handlers.get(command)
    if handler is None:
        raise HTTPException(status_code=404, detail="Unknown command")
    try:
        return handler(mission_id)
    except MissionNotFound as exc:
        raise HTTPException(status_code=404, detail="Mission not found") from exc
    except EngineBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidState as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/routines")
async def list_routines():
    return {"routines": _engine().list_routines()}


@app.post("/api/routines/{routine_id}/{command}")
async def routine_command(routine_id: str, command: str):
    current = _engine()
    try:
        if command == "run":
            return current.run_routine(routine_id)
        if command == "toggle":
            return current.toggle_routine(routine_id)
        if command == "delete":
            current.delete_routine(routine_id)
            return {"deleted": routine_id}
    except MissionNotFound as exc:
        raise HTTPException(status_code=404, detail="Routine not found") from exc
    except (EngineBusy, InvalidState) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="Unknown command")


@app.post("/api/voice/transcribe")
async def transcribe(request: Request):
    audio = await request.body()
    try:
        return await voice.transcribe(audio)
    except MacError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/voice/say")
async def say(request: SayRequest):
    await voice.say(request.text, wait=request.wait)
    return {"ok": True}


@app.post("/api/voice/stop")
async def stop_speaking():
    await voice.stop_speaking()
    return {"ok": True}


@app.get("/api/stream")
async def stream(request: Request):
    queue = _engine().subscribe()

    async def events():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            _engine().unsubscribe(queue)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/api/captures/{name}")
async def capture(name: str):
    if not re.fullmatch(r"[a-f0-9]{12}-\d{1,2}\.png", name):
        raise HTTPException(status_code=404, detail="Not found")
    path = CAPTURE_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Capture no longer available")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
