"""Mission lifecycle: plan → (approval) → act → verify → optional undo."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterator

import intents
import llm
import planner
from actions import CATALOG, ONE_WAY, READ, Action, ArgError, PlanContext, RunContext, StepResult
from macos import MacError, delete_captures, permission_help, permissions
from schedule import Schedule, extract
from store import MissionStore, RoutineStore

ACTIVE_STATES = {"planning", "running", "undoing"}
LOG_LIMIT = 400
OUTPUT_LIMIT = 8000
# A routine whose time passed while the Mac was asleep or off is skipped, not run late.
MISSED_AFTER = timedelta(hours=2)

PlanFn = Callable[..., Awaitable[tuple[planner.Plan, list[llm.LLMResult]]]]
PermissionFn = Callable[[], Awaitable[dict[str, bool]]]


class EngineBusy(RuntimeError):
    pass


class MissionNotFound(KeyError):
    pass


class InvalidState(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _substitute(args: dict[str, Any], outputs: dict[int, str]) -> tuple[dict[str, Any], bool]:
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return outputs.get(int(match.group(1)), "")

    result = {
        key: re.sub(r"\{\{step(\d+)\}\}", replace, value) if isinstance(value, str) else value
        for key, value in args.items()
    }
    return result, changed


def _step_record(index: int, action: str, args: dict[str, Any], label: str, risk: str, app: str | None, produces_text: bool) -> dict[str, Any]:
    return {
        "index": index,
        "action": action,
        "args": args,
        "label": label,
        "risk": risk,
        "app": app,
        "produces_text": produces_text,
        "status": "pending",
        "output": "",
        "evidence": "",
        "error": None,
        "artifact": None,
        "undo": None,
        "undo_evidence": "",
        "duration_ms": None,
    }


class Engine:
    def __init__(
        self,
        store: MissionStore,
        catalog: dict[str, Action] | None = None,
        plan_fn: PlanFn | None = None,
        routines: RoutineStore | None = None,
        permissions_fn: PermissionFn | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog or CATALOG
        self.plan_fn = plan_fn or planner.make_plan
        self.permissions_fn = permissions_fn or (lambda: permissions(fresh=True))
        self.routines = routines
        self.active_id: str | None = None
        self._abort: set[str] = set()
        self._subscribers: set[asyncio.Queue] = set()
        self._tasks: set[asyncio.Task] = set()
        self._recover_interrupted()

    # ------------------------------------------------------------------ events

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    def _commit(self, mission: dict[str, Any]) -> None:
        mission["updated_at"] = _now()
        self.store.save(mission)
        self._publish({"type": "mission", "mission": self.public(mission)})

    def _log(self, mission: dict[str, Any], kind: str, message: str, step: int | None = None) -> None:
        mission["log"].append({"t": _now(), "kind": kind, "message": message, "step": step})
        del mission["log"][:-LOG_LIMIT]
        self._commit(mission)

    def public(self, mission: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in mission.items() if key != "steps"}
        view["steps"] = [
            {key: value for key, value in step.items() if key != "undo"} | {"has_undo": bool(step.get("undo"))}
            for step in mission["steps"]
        ]
        view["can_undo"] = self._undoable(mission) and mission["status"] not in ACTIVE_STATES
        return view

    @staticmethod
    def _undoable(mission: dict[str, Any]) -> bool:
        return any(
            step.get("undo") and step["status"] in {"verified", "failed"} for step in mission["steps"]
        )

    # ------------------------------------------------------------------ queries

    def get(self, mission_id: str) -> dict[str, Any]:
        mission = self.store.get(mission_id)
        if mission is None:
            raise MissionNotFound(mission_id)
        return mission

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return [self.public(m) for m in self.store.recent(limit)]

    # ------------------------------------------------------------------ commands

    def _claim(self, mission_id: str) -> None:
        if self.active_id is not None:
            raise EngineBusy("Human Emulator is already working on a mission.")
        self.active_id = mission_id

    @contextmanager
    def _claimed(self, mission_id: str) -> Iterator[None]:
        """Claim the engine, releasing it if anything fails before the task is spawned."""
        self._claim(mission_id)
        try:
            yield
        except BaseException:
            if self.active_id == mission_id:
                self.active_id = None
            raise

    def _spawn(self, coroutine: Awaitable[None], mission_id: str) -> None:
        async def runner() -> None:
            try:
                await coroutine
            finally:
                if self.active_id == mission_id:
                    self.active_id = None
                self._publish({"type": "engine", "busy": self.active_id is not None})

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def create(self, goal: str) -> dict[str, Any]:
        goal = goal.strip()
        if not goal:
            raise ValueError("Describe what you want Human Emulator to do.")
        if intents.is_noise(goal):
            raise ValueError("I didn't catch that — tell me what to do, e.g. “say hi to Sam on WhatsApp”.")
        schedule, task = extract(goal) if self.routines is not None else (None, goal)
        if schedule is not None and not task:
            raise ValueError(f"{schedule.describe()} — and do what? Add the task, e.g. “every day at 9am brief my day”.")
        mission_id = uuid.uuid4().hex[:12]
        mission: dict[str, Any] = {
            "id": mission_id,
            "goal": goal[:2000],
            "plan_goal": task[:2000],
            "title": "New mission",
            "status": "planning",
            "created_at": _now(),
            "updated_at": _now(),
            "model": llm.MODEL,
            "unsupported": "",
            "warnings": [],
            "error": None,
            "steps": [],
            "log": [],
            "result": None,
            "telemetry": {},
            "routine": {"schedule": schedule.to_dict(), "describe": schedule.describe()} if schedule else None,
        }
        with self._claimed(mission_id):
            self._commit(mission)
            self._spawn(self._plan(mission), mission_id)
        return self.public(mission)

    def approve(self, mission_id: str) -> dict[str, Any]:
        mission = self.get(mission_id)
        if mission["status"] != "awaiting_approval":
            raise InvalidState(f"Mission is {mission['status']}, not awaiting approval.")
        if mission.get("routine"):
            routine = self._save_routine(mission, Schedule.from_dict(mission["routine"]["schedule"]))
            mission["routine"]["id"] = routine["id"]
            mission["status"] = "scheduled"
            for step in mission["steps"]:
                step["status"] = "scheduled"
            when = datetime.fromtimestamp(routine["next_run_at"]).strftime("%a %-d %b, %-I:%M %p")
            self._log(mission, "done", f"Routine saved — {routine['describe']}. First run {when}. Nothing ran now.")
            return self.public(mission)
        with self._claimed(mission_id):
            mission["status"] = "running"
            self._log(mission, "info", "Plan approved by you.")
            self._spawn(self._run(mission), mission_id)
        return self.public(mission)

    def cancel(self, mission_id: str) -> dict[str, Any]:
        mission = self.get(mission_id)
        if mission["status"] != "awaiting_approval":
            raise InvalidState(f"Mission is {mission['status']}; only plans awaiting approval can be cancelled.")
        mission["status"] = "cancelled"
        for step in mission["steps"]:
            step["status"] = "skipped"
        self._log(mission, "info", "Plan rejected — nothing was changed on this Mac.")
        return self.public(mission)

    def abort(self, mission_id: str) -> dict[str, Any]:
        mission = self.get(mission_id)
        if mission["status"] != "running" or self.active_id != mission_id:
            raise InvalidState("Only a running mission can be stopped.")
        self._abort.add(mission_id)
        return self.public(mission)

    def undo(self, mission_id: str) -> dict[str, Any]:
        mission = self.get(mission_id)
        if mission["status"] in ACTIVE_STATES or not self._undoable(mission):
            raise InvalidState("This mission has nothing left to undo.")
        with self._claimed(mission_id):
            mission["status"] = "undoing"
            self._log(mission, "undo", "Rolling back the mission in reverse order…")
            self._spawn(self._undo(mission), mission_id)
        return self.public(mission)

    # ------------------------------------------------------------------ deleting

    def _deletable(self, mission: dict[str, Any]) -> bool:
        return mission["status"] not in ACTIVE_STATES and mission["id"] != self.active_id

    def delete(self, mission_id: str) -> dict[str, Any]:
        """Remove a finished mission and its screenshots. Undo-bin files are kept."""
        mission = self.get(mission_id)
        if not self._deletable(mission):
            raise InvalidState("This mission is still working — stop it or wait for it to finish, then delete it.")
        self.store.delete([mission_id])
        delete_captures(mission_id)
        self._publish({"type": "deleted", "ids": [mission_id]})
        return {"deleted": [mission_id]}

    def clear(self) -> dict[str, Any]:
        """Delete every finished mission; a mission that is still working is kept."""
        ids = [m["id"] for m in self.store.all() if self._deletable(m)]
        self.store.delete(ids)
        for mission_id in ids:
            delete_captures(mission_id)
        self._publish({"type": "deleted", "ids": ids})
        return {"deleted": ids}

    # ------------------------------------------------------------------ planning

    async def _plan(self, mission: dict[str, Any]) -> None:
        mission_id = mission["id"]
        self._log(mission, "decide", "Understanding your request…")

        async def on_delta(text: str) -> None:
            self._publish({"type": "token", "mission_id": mission_id, "phase": "plan", "text": text})

        started = time.monotonic()
        try:
            plan, runs = await self.plan_fn(mission.get("plan_goal") or mission["goal"], datetime.now(), on_delta=on_delta)
        except llm.LLMError as exc:
            mission["status"] = "failed"
            mission["error"] = str(exc)
            self._log(mission, "error", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - surface any planner crash to the user
            mission["status"] = "failed"
            mission["error"] = f"Planner crashed: {exc}"
            self._log(mission, "error", mission["error"])
            return

        tokens = sum(r.tokens for r in runs)
        rates = [r.tokens_per_s for r in runs if r.tokens_per_s]
        mission["telemetry"] = {
            "plan_ms": int((time.monotonic() - started) * 1000),
            "plan_tokens": tokens,
            "tokens_per_s": round(sum(rates) / len(rates), 1) if rates else None,
            "plan_passes": len(runs),
        }
        mission["title"] = plan.title
        mission["unsupported"] = plan.unsupported
        mission["warnings"] = plan.warnings
        mission["steps"] = [
            _step_record(
                index,
                step.action,
                step.args,
                step.label,
                step.risk,
                step.app,
                self.catalog[step.action].produces_text if step.action in self.catalog else step.produces_text,
            )
            for index, step in enumerate(plan.steps)
        ]
        if runs:
            self._log(
                mission,
                "decide",
                f"Plan ready from the on-device model ({llm.MODEL}): {len(plan.steps)} step(s) from {tokens} tokens in "
                f"{mission['telemetry']['plan_ms'] / 1000:.1f}s"
                + (f" at {mission['telemetry']['tokens_per_s']} tok/s." if rates else "."),
            )
        elif plan.steps:
            self._log(mission, "decide", f"Plan ready instantly: {len(plan.steps)} step(s), no model call needed.")
        for note in plan.notes:
            self._log(mission, "decide", note)
        for warning in plan.warnings:
            self._log(mission, "warn", warning)
        if plan.unsupported:
            self._log(mission, "warn", plan.unsupported)

        if not plan.steps:
            mission["status"] = "rejected" if plan.unsupported else "failed"
            if not plan.unsupported:
                mission["error"] = "Human Emulator could not map this goal onto its audited actions."
            self._log(mission, "info", "Nothing to run.")
            return

        missing = await self._missing_permissions(plan.steps)
        if missing:
            mission["status"] = "failed"
            mission["needs"] = missing
            mission["error"] = permission_help(missing)
            for step in mission["steps"]:
                step["status"] = "skipped"
            self._log(mission, "error", f"Stopped before asking for approval: {mission['error']}")
            return

        if mission.get("routine"):
            mission["status"] = "awaiting_approval"
            self._log(
                mission,
                "info",
                f"Routine: {mission['routine']['describe']}. Approving saves this exact plan to replay on schedule — it will not run now.",
            )
            return

        if all(step.risk == READ for step in plan.steps):
            mission["status"] = "running"
            self._log(mission, "info", "Read-only plan — running without approval.")
            await self._run(mission)
            return

        mission["status"] = "awaiting_approval"
        changes = sum(1 for step in plan.steps if step.risk != READ)
        self._log(mission, "info", f"Waiting for your approval: {changes} step(s) will change this Mac.")

    # ------------------------------------------------------------------ execution

    async def _missing_permissions(self, steps: list[Any]) -> list[str]:
        needed = sorted({need for step in steps for need in getattr(self.catalog.get(step.action), "requires", ())})
        if not needed:
            return []
        try:
            granted = await self.permissions_fn()
        except Exception:  # noqa: BLE001 - a broken check must not block missions
            return []
        return [need for need in needed if not granted.get(need)]

    async def _run(self, mission: dict[str, Any]) -> None:
        mission_id = mission["id"]
        started = time.monotonic()
        outputs: dict[int, str] = {}
        known_folders: list = []
        failed = False

        for step in mission["steps"]:
            position = step["index"] + 1
            if failed or mission_id in self._abort:
                step["status"] = "skipped"
                continue
            action = self.catalog[step["action"]]

            async def emit(kind: str, message: str, _position: int = position) -> None:
                self._log(mission, kind, message, _position)

            async def on_delta(text: str, _position: int = position) -> None:
                self._publish(
                    {"type": "token", "mission_id": mission_id, "phase": "step", "step": _position, "text": text}
                )

            ctx = RunContext(
                mission_id=mission_id,
                step_index=step["index"],
                now=datetime.now(),
                known_folders=known_folders,
                emit=emit,
                on_delta=on_delta,
                goal=mission["goal"],
            )
            step["status"] = "running"
            self._log(mission, "act", step["label"], position)
            step_started = time.monotonic()
            try:
                args, changed = _substitute(step["args"], outputs)
                if changed:
                    args = action.normalize(action.canonical_args(args), PlanContext(now=ctx.now, known_folders=list(known_folders)))
                    step["resolved_preview"] = {
                        k: (v[:240] + "…" if isinstance(v, str) and len(v) > 240 else v) for k, v in args.items()
                    }
                result: StepResult = await action.run(args, ctx)
                step["undo"] = result.undo
                step["artifact"] = result.artifact
                step["output"] = result.output[:OUTPUT_LIMIT]
                outputs[position] = result.output
                step["status"] = "verifying"
                self._log(mission, "act", result.evidence or "Done.", position)
                check = await action.verify(args, result, ctx)
                step["evidence"] = check.evidence
                step["status"] = "verified" if check.ok else "failed"
                if not check.ok:
                    step["error"] = check.evidence
                    failed = True
                self._log(mission, "verify" if check.ok else "error", check.evidence, position)
            except (ArgError, MacError, llm.LLMError, OSError) as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
                failed = True
                self._log(mission, "error", f"{step['label']} failed: {exc}", position)
            except Exception as exc:  # noqa: BLE001 - never leave a mission stuck running
                step["status"] = "failed"
                step["error"] = f"Unexpected error: {exc}"
                failed = True
                self._log(mission, "error", f"{step['label']} crashed: {exc}", position)
            step["duration_ms"] = int((time.monotonic() - step_started) * 1000)

        verified = sum(1 for s in mission["steps"] if s["status"] == "verified")
        mission["telemetry"]["run_ms"] = int((time.monotonic() - started) * 1000)
        mission["telemetry"]["verified"] = verified
        text_steps = [s for s in mission["steps"] if s["produces_text"] and s["status"] == "verified" and s["output"]]
        if text_steps:
            mission["result"] = {"text": text_steps[-1]["output"], "step": text_steps[-1]["index"] + 1}

        if mission_id in self._abort:
            self._abort.discard(mission_id)
            mission["status"] = "aborted"
            self._log(mission, "warn", "Stopped by you. Completed steps can still be undone.")
        elif failed:
            mission["status"] = "failed"
            mission["error"] = next((s["error"] for s in mission["steps"] if s["status"] == "failed"), None)
            self._log(mission, "error", f"Mission halted safely: {verified}/{len(mission['steps'])} steps verified. Remaining steps were skipped.")
        else:
            mission["status"] = "completed"
            self._log(
                mission,
                "done",
                f"Mission complete: {verified}/{len(mission['steps'])} steps verified in "
                f"{mission['telemetry']['run_ms'] / 1000:.1f}s.",
            )

    # ------------------------------------------------------------------ undo

    async def _undo(self, mission: dict[str, Any]) -> None:
        mission_id = mission["id"]
        all_ok = True
        for step in reversed(mission["steps"]):
            position = step["index"] + 1
            if not step.get("undo") or step["status"] not in {"verified", "failed"}:
                if step["status"] == "verified" and step["risk"] == ONE_WAY:
                    self._log(mission, "warn", f"Step {position} cannot be undone ({step['label']}).", position)
                continue
            action = self.catalog[step["action"]]

            async def emit(kind: str, message: str, _position: int = position) -> None:
                self._log(mission, kind, message, _position)

            ctx = RunContext(
                mission_id=mission_id, step_index=step["index"], now=datetime.now(), known_folders=[], emit=emit
            )
            step["status"] = "undoing"
            self._log(mission, "undo", f"Undoing: {step['label']}", position)
            try:
                check = await action.undo(step["undo"], ctx)
            except (MacError, OSError) as exc:
                check = None
                step["status"] = "undo_failed"
                step["undo_evidence"] = str(exc)
                all_ok = False
                self._log(mission, "error", f"Undo failed: {exc}", position)
            if check is not None:
                step["undo_evidence"] = check.evidence
                if check.ok:
                    step["status"] = "undone"
                    step["undo"] = None
                    self._log(mission, "verify", check.evidence, position)
                else:
                    step["status"] = "kept"
                    step["undo"] = None
                    all_ok = False
                    self._log(mission, "warn", check.evidence, position)
        mission["status"] = "undone" if all_ok else "partially_undone"
        self._log(
            mission,
            "done",
            "Rollback complete — this Mac is back to how it was." if all_ok else "Rollback finished with items kept for safety.",
        )

    # ------------------------------------------------------------------ routines

    def _routine_store(self) -> RoutineStore:
        if self.routines is None:
            raise InvalidState("Routines are not enabled.")
        return self.routines

    def _save_routine(self, mission: dict[str, Any], schedule: Schedule) -> dict[str, Any]:
        store = self._routine_store()
        routine = {
            "id": uuid.uuid4().hex[:12],
            "name": mission["title"],
            "goal": mission.get("plan_goal") or mission["goal"],
            "schedule": schedule.to_dict(),
            "describe": schedule.describe(),
            "steps": [
                {key: step[key] for key in ("action", "args", "label", "risk", "app", "produces_text")}
                for step in mission["steps"]
            ],
            "enabled": True,
            "created_at": _now(),
            "source_mission_id": mission["id"],
            "next_run_at": schedule.next_after(datetime.now()).timestamp(),
            "last_run_at": None,
            "last_mission_id": None,
            "last_error": None,
            "runs": 0,
            "missed": 0,
        }
        store.save(routine)
        self._publish({"type": "routines"})
        return routine

    def save_as_routine(self, mission_id: str, phrase: str) -> dict[str, Any]:
        """Turn a mission that already worked into a routine ("every weekday at 8:30")."""
        mission = self.get(mission_id)
        if mission["status"] not in {"completed", "undone"} or not mission["steps"]:
            raise InvalidState("Only a mission that completed (or was undone) can become a routine.")
        schedule, rest = extract(phrase)
        if schedule is None or rest:
            raise ValueError("Describe only the schedule, e.g. “every weekday at 8:30am” or “every 2 hours”.")
        return self.public_routine(self._save_routine(mission, schedule))

    def run_routine(self, routine_id: str, *, scheduled: bool = False, now: datetime | None = None) -> dict[str, Any]:
        """Replay a routine's approved steps now — no model call, every step still verified."""
        store = self._routine_store()
        routine = store.get(routine_id)
        if routine is None:
            raise MissionNotFound(routine_id)
        now = now or datetime.now()
        ctx = PlanContext(now=now)
        steps = []
        try:
            for index, saved in enumerate(routine["steps"]):
                action = self.catalog[saved["action"]]
                args = action.normalize(action.canonical_args(saved["args"]), ctx)
                label = re.sub(r"\{\{step(\d+)\}\}", r"result of step \1", action.describe(args))
                steps.append(_step_record(index, action.name, args, label, action.risk, action.app, action.produces_text))
        except (KeyError, ArgError) as exc:
            routine["last_error"] = f"Step {len(steps) + 1} is no longer valid: {exc}"
            store.save(routine)
            self._publish({"type": "routines"})
            raise InvalidState(routine["last_error"]) from exc
        mission_id = uuid.uuid4().hex[:12]
        approved = datetime.fromtimestamp(routine["created_at"]).strftime("%-d %b")
        mission: dict[str, Any] = {
            "id": mission_id,
            "goal": routine["goal"],
            "plan_goal": routine["goal"],
            "title": routine["name"],
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "model": llm.MODEL,
            "unsupported": "",
            "warnings": [],
            "error": None,
            "steps": steps,
            "log": [],
            "result": None,
            "telemetry": {"plan_ms": 0, "replay": True},
            "routine": {"id": routine["id"], "describe": routine["describe"], "run": True},
        }
        with self._claimed(mission_id):
            self._log(
                mission,
                "decide",
                f"Routine “{routine['name']}” ({routine['describe']}) — replaying the plan you approved on {approved}; no model call.",
            )
            routine["last_run_at"] = _now()
            routine["last_mission_id"] = mission_id
            routine["last_error"] = None
            routine["runs"] = routine.get("runs", 0) + 1
            if scheduled:
                routine["next_run_at"] = Schedule.from_dict(routine["schedule"]).next_after(now).timestamp()
            store.save(routine)
            self._publish({"type": "routines"})
            self._spawn(self._run(mission), mission_id)
        return self.public(mission)

    async def tick_routines(self, now: datetime | None = None) -> None:
        """Start any routine that is due. Called by the server every few seconds."""
        if self.routines is None:
            return
        now = now or datetime.now()
        for routine in self.routines.all():
            if not routine.get("enabled") or routine.get("next_run_at") is None:
                continue
            due = datetime.fromtimestamp(routine["next_run_at"])
            if due > now:
                continue
            if now - due > MISSED_AFTER:
                routine["missed"] = routine.get("missed", 0) + 1
                routine["next_run_at"] = Schedule.from_dict(routine["schedule"]).next_after(now).timestamp()
                self.routines.save(routine)
                self._publish({"type": "routines"})
                continue
            if self.active_id is not None:
                continue  # busy — try again on the next tick
            try:
                self.run_routine(routine["id"], scheduled=True, now=now)
            except EngineBusy:
                continue
            except Exception as exc:  # noqa: BLE001 - one bad routine must not stall the scheduler
                routine = self.routines.get(routine["id"]) or routine
                routine["last_error"] = routine.get("last_error") or f"Could not start: {exc}"
                try:
                    routine["next_run_at"] = Schedule.from_dict(routine["schedule"]).next_after(now).timestamp()
                except (KeyError, ValueError):
                    routine["enabled"] = False
                self.routines.save(routine)
                self._publish({"type": "routines"})

    def public_routine(self, routine: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in routine.items() if key != "steps"}
        view["step_labels"] = [step["label"] for step in routine["steps"]]
        view["risks"] = sorted({step["risk"] for step in routine["steps"]})
        last = self.store.get(routine["last_mission_id"]) if routine.get("last_mission_id") else None
        view["last_status"] = last["status"] if last else None
        return view

    def list_routines(self) -> list[dict[str, Any]]:
        return [self.public_routine(r) for r in self._routine_store().all()]

    def toggle_routine(self, routine_id: str) -> dict[str, Any]:
        store = self._routine_store()
        routine = store.get(routine_id)
        if routine is None:
            raise MissionNotFound(routine_id)
        routine["enabled"] = not routine.get("enabled", True)
        if routine["enabled"]:
            routine["next_run_at"] = Schedule.from_dict(routine["schedule"]).next_after(datetime.now()).timestamp()
        store.save(routine)
        self._publish({"type": "routines"})
        return self.public_routine(routine)

    def delete_routine(self, routine_id: str) -> None:
        if not self._routine_store().delete(routine_id):
            raise MissionNotFound(routine_id)
        self._publish({"type": "routines"})

    # ------------------------------------------------------------------ recovery

    def _recover_interrupted(self) -> None:
        for mission in self.store.recent(200):
            if mission["status"] not in ACTIVE_STATES:
                continue
            for step in mission["steps"]:
                if step["status"] in {"running", "verifying", "undoing"}:
                    step["status"] = "failed"
                    step["error"] = "Interrupted when Human Emulator stopped."
                elif step["status"] == "pending":
                    step["status"] = "skipped"
            mission["status"] = "interrupted"
            mission["log"].append({"t": _now(), "kind": "warn", "message": "Human Emulator restarted mid-mission.", "step": None})
            self.store.save(mission)
