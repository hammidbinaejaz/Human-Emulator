import asyncio
from pathlib import Path

import pytest

import llm
import planner
from actions import ONE_WAY, READ, REVERSIBLE, Action, Check, StepResult
from engine import Engine, EngineBusy, InvalidState
from store import MissionStore


class Recorder:
    def __init__(self):
        self.calls: list[str] = []


class FakeAction(Action):
    def __init__(self, name, risk, recorder, *, verify_ok=True, output=""):
        self.name = name
        self.label = name
        self.risk = risk
        self.produces_text = bool(output)
        self.recorder = recorder
        self.verify_ok = verify_ok
        self.output = output

    def normalize(self, args, ctx):
        return dict(args)

    def describe(self, args):
        return self.name

    async def run(self, args, ctx):
        self.recorder.calls.append(f"run:{self.name}:{args.get('text', '')}")
        undo = {"name": self.name} if self.risk == REVERSIBLE else None
        return StepResult(output=self.output, evidence=f"{self.name} ran", undo=undo)

    async def verify(self, args, result, ctx):
        return Check(self.verify_ok, f"{self.name} {'ok' if self.verify_ok else 'mismatch'}")

    async def undo(self, data, ctx):
        self.recorder.calls.append(f"undo:{data['name']}")
        return Check(True, f"{data['name']} undone")


def make_plan_fn(*steps, unsupported=""):
    async def plan_fn(goal, now, on_delta=None):
        if on_delta:
            await on_delta('{"title":')
        planned = [
            planner.PlannedStep(action=name, args=args, label=name, risk=risk, app=None, produces_text=False)
            for name, risk, args in steps
        ]
        return planner.Plan(title="Test", steps=planned, unsupported=unsupported), [
            llm.LLMResult(text="{}", tokens=12, tokens_per_s=6.0, duration_ms=50)
        ]

    return plan_fn


async def settle(engine: Engine) -> None:
    while engine._tasks:
        await asyncio.gather(*list(engine._tasks))


def build(tmp_path: Path, catalog, plan_fn) -> Engine:
    return Engine(MissionStore(tmp_path / "m.db"), catalog=catalog, plan_fn=plan_fn)


def test_reversible_plan_waits_for_approval_then_runs_and_undoes_in_reverse(tmp_path):
    rec = Recorder()
    catalog = {n: FakeAction(n, REVERSIBLE, rec) for n in ("a", "b", "c")}
    engine = build(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {}), ("b", REVERSIBLE, {}), ("c", REVERSIBLE, {})))

    async def scenario():
        mission = engine.create("do things")
        await settle(engine)
        assert engine.get(mission["id"])["status"] == "awaiting_approval"
        assert rec.calls == []

        engine.approve(mission["id"])
        await settle(engine)
        done = engine.get(mission["id"])
        assert done["status"] == "completed"
        assert [s["status"] for s in done["steps"]] == ["verified"] * 3
        assert engine.public(done)["can_undo"] is True

        engine.undo(mission["id"])
        await settle(engine)
        undone = engine.get(mission["id"])
        assert undone["status"] == "undone"
        assert rec.calls[-3:] == ["undo:c", "undo:b", "undo:a"]
        assert engine.public(undone)["can_undo"] is False

    asyncio.run(scenario())


def test_failed_verification_halts_and_skips_the_rest(tmp_path):
    rec = Recorder()
    catalog = {
        "a": FakeAction("a", REVERSIBLE, rec),
        "bad": FakeAction("bad", REVERSIBLE, rec, verify_ok=False),
        "c": FakeAction("c", REVERSIBLE, rec),
    }
    engine = build(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {}), ("bad", REVERSIBLE, {}), ("c", REVERSIBLE, {})))

    async def scenario():
        mission = engine.create("x")
        await settle(engine)
        engine.approve(mission["id"])
        await settle(engine)
        result = engine.get(mission["id"])
        assert result["status"] == "failed"
        assert [s["status"] for s in result["steps"]] == ["verified", "failed", "skipped"]
        assert "run:c:" not in rec.calls

        engine.undo(mission["id"])
        await settle(engine)
        assert rec.calls[-2:] == ["undo:bad", "undo:a"]

    asyncio.run(scenario())


def test_read_only_plan_runs_without_approval_and_passes_output_forward(tmp_path):
    rec = Recorder()
    catalog = {
        "look": FakeAction("look", READ, rec, output="screen text"),
        "say": FakeAction("say", ONE_WAY, rec),
    }
    engine = build(tmp_path, catalog, make_plan_fn(("look", READ, {})))

    async def scenario():
        mission = engine.create("read")
        await settle(engine)
        result = engine.get(mission["id"])
        assert result["status"] == "completed"
        assert result["result"]["text"] == "screen text"

    asyncio.run(scenario())

    engine2 = build(tmp_path, catalog, make_plan_fn(("look", READ, {}), ("say", ONE_WAY, {"text": "{{step1}}"})))

    async def chained():
        mission = engine2.create("read and say")
        await settle(engine2)
        engine2.approve(mission["id"])
        await settle(engine2)
        assert "run:say:screen text" in rec.calls

    asyncio.run(chained())


def test_only_one_mission_at_a_time_and_invalid_commands_are_refused(tmp_path):
    rec = Recorder()
    catalog = {"a": FakeAction("a", REVERSIBLE, rec)}
    engine = build(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {})))

    async def scenario():
        first = engine.create("one")
        with pytest.raises(EngineBusy):
            engine.create("two")
        await settle(engine)
        with pytest.raises(InvalidState):
            engine.undo(first["id"])
        engine.cancel(first["id"])
        assert engine.get(first["id"])["status"] == "cancelled"
        with pytest.raises(InvalidState):
            engine.approve(first["id"])
        assert rec.calls == []

    asyncio.run(scenario())


def test_out_of_scope_goal_is_rejected_without_running(tmp_path):
    engine = build(tmp_path, {}, make_plan_fn(unsupported="Sending messages is outside Case's audited actions."))

    async def scenario():
        mission = engine.create("WhatsApp Mom")
        await settle(engine)
        assert engine.get(mission["id"])["status"] == "rejected"

    asyncio.run(scenario())


def test_interrupted_missions_are_recovered_on_restart(tmp_path):
    store = MissionStore(tmp_path / "m.db")
    store.save(
        {
            "id": "abc", "goal": "g", "title": "t", "status": "running", "created_at": 1.0, "updated_at": 1.0,
            "steps": [
                {"index": 0, "status": "verified", "undo": {"x": 1}, "risk": REVERSIBLE},
                {"index": 1, "status": "running", "undo": None, "risk": REVERSIBLE},
                {"index": 2, "status": "pending", "undo": None, "risk": REVERSIBLE},
            ],
            "log": [],
        }
    )
    engine = Engine(store, catalog={}, plan_fn=make_plan_fn())
    recovered = engine.get("abc")
    assert recovered["status"] == "interrupted"
    assert [s["status"] for s in recovered["steps"]] == ["verified", "failed", "skipped"]
    assert engine._undoable(recovered)


def build_with_routines(tmp_path: Path, catalog, plan_fn) -> Engine:
    from store import RoutineStore

    return Engine(MissionStore(tmp_path / "m.db"), catalog=catalog, plan_fn=plan_fn, routines=RoutineStore(tmp_path / "m.db"))


def test_routine_goal_is_approved_once_and_saved_without_running(tmp_path):
    rec = Recorder()
    catalog = {"agenda": FakeAction("agenda", READ, rec, output="2 events"), "say": FakeAction("say", ONE_WAY, rec)}
    calls = []
    plan_fn = make_plan_fn(("agenda", READ, {}), ("say", ONE_WAY, {"text": "{{step1}}"}))

    async def spy(goal, now, on_delta=None):
        calls.append(goal)
        return await plan_fn(goal, now, on_delta)

    engine = build_with_routines(tmp_path, catalog, spy)

    async def scenario():
        mission = engine.create("Every day at 9am, tell me my agenda out loud")
        await settle(engine)
        assert calls == ["Tell me my agenda out loud"]
        assert engine.get(mission["id"])["status"] == "awaiting_approval"
        approved = engine.approve(mission["id"])
        assert approved["status"] == "scheduled"
        assert rec.calls == []
        (routine,) = engine.list_routines()
        assert routine["describe"] == "Every day at 9:00 AM"
        assert routine["step_labels"] == ["agenda", "say"]
        assert routine["enabled"] is True

    asyncio.run(scenario())


def test_routine_replays_approved_steps_without_the_model(tmp_path):
    rec = Recorder()
    catalog = {"a": FakeAction("a", REVERSIBLE, rec), "b": FakeAction("b", REVERSIBLE, rec)}
    calls = []

    async def plan_fn(goal, now, on_delta=None):
        calls.append(goal)
        return await make_plan_fn(("a", REVERSIBLE, {}), ("b", REVERSIBLE, {}))(goal, now, on_delta)

    engine = build_with_routines(tmp_path, catalog, plan_fn)

    async def scenario():
        mission = engine.create("every weekday at 8:30am do a and b")
        await settle(engine)
        engine.approve(mission["id"])
        routine_id = engine.list_routines()[0]["id"]
        run = engine.run_routine(routine_id)
        await settle(engine)
        finished = engine.get(run["id"])
        assert finished["status"] == "completed"
        assert finished["telemetry"]["replay"] is True
        assert rec.calls == ["run:a:", "run:b:"]
        assert len(calls) == 1  # planned once, replayed without the model
        assert engine.list_routines()[0]["runs"] == 1
        assert engine.list_routines()[0]["last_status"] == "completed"
        engine.undo(run["id"])
        await settle(engine)
        assert rec.calls[-2:] == ["undo:b", "undo:a"]

    asyncio.run(scenario())


def test_scheduler_runs_due_routines_and_skips_missed_ones(tmp_path):
    from datetime import datetime, timedelta

    rec = Recorder()
    catalog = {"a": FakeAction("a", REVERSIBLE, rec)}
    engine = build_with_routines(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {})))

    async def scenario():
        mission = engine.create("every 2 hours do a")
        await settle(engine)
        engine.approve(mission["id"])
        routine = engine.routines.all()[0]
        due = datetime.fromtimestamp(routine["next_run_at"])

        await engine.tick_routines(due - timedelta(minutes=1))
        await settle(engine)
        assert rec.calls == []

        await engine.tick_routines(due + timedelta(seconds=20))
        await settle(engine)
        assert rec.calls == ["run:a:"]
        routine = engine.routines.all()[0]
        assert datetime.fromtimestamp(routine["next_run_at"]) > due

        late = datetime.fromtimestamp(routine["next_run_at"]) + timedelta(hours=3)
        await engine.tick_routines(late)
        await settle(engine)
        assert rec.calls == ["run:a:"]
        assert engine.routines.all()[0]["missed"] == 1

    asyncio.run(scenario())


def test_completed_mission_can_be_saved_as_a_routine_and_toggled(tmp_path):
    rec = Recorder()
    catalog = {"a": FakeAction("a", REVERSIBLE, rec)}
    engine = build_with_routines(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {})))

    async def scenario():
        mission = engine.create("do a")
        await settle(engine)
        with pytest.raises(InvalidState):
            engine.save_as_routine(mission["id"], "every day at 9am")
        engine.approve(mission["id"])
        await settle(engine)
        with pytest.raises(ValueError):
            engine.save_as_routine(mission["id"], "tomorrow at 9am")
        routine = engine.save_as_routine(mission["id"], "every weekday at 8:30am")
        assert routine["describe"] == "Every weekday at 8:30 AM"
        assert engine.toggle_routine(routine["id"])["enabled"] is False
        engine.delete_routine(routine["id"])
        assert engine.list_routines() == []

    asyncio.run(scenario())


def test_engine_is_released_when_starting_a_mission_fails(tmp_path, monkeypatch):
    rec = Recorder()
    catalog = {"a": FakeAction("a", REVERSIBLE, rec)}
    engine = build_with_routines(tmp_path, catalog, make_plan_fn(("a", REVERSIBLE, {})))

    async def scenario():
        def broken_save(mission):
            raise OSError("disk full")

        monkeypatch.setattr(engine.store, "save", broken_save)
        with pytest.raises(OSError):
            engine.create("do a")
        assert engine.active_id is None

    asyncio.run(scenario())


def test_finished_missions_can_be_deleted_and_cleared_but_not_running_ones(tmp_path, monkeypatch):
    import engine as engine_module

    removed = []
    monkeypatch.setattr(engine_module, "delete_captures", lambda mission_id: removed.append(mission_id) or 0)
    rec = Recorder()
    gate = asyncio.Event()

    class Slow(FakeAction):
        async def run(self, args, ctx):
            await gate.wait()
            return await super().run(args, ctx)

    catalog = {"a": FakeAction("a", REVERSIBLE, rec), "slow": Slow("slow", READ, rec)}
    plans = iter([make_plan_fn(("a", REVERSIBLE, {})), make_plan_fn(("a", REVERSIBLE, {})), make_plan_fn(("slow", READ, {}))])

    async def plan_fn(goal, now, on_delta=None):
        return await next(plans)(goal, now, on_delta)

    engine = build(tmp_path, catalog, plan_fn)

    async def scenario():
        first = engine.create("one")
        await settle(engine)
        second = engine.create("two")
        await settle(engine)
        assert engine.delete(first["id"]) == {"deleted": [first["id"]]}
        assert removed == [first["id"]]
        running = engine.create("slow one")
        await asyncio.sleep(0.05)
        assert engine.get(running["id"])["status"] == "running"
        with pytest.raises(InvalidState):
            engine.delete(running["id"])
        assert engine.clear() == {"deleted": [second["id"]]}
        assert [m["id"] for m in engine.recent()] == [running["id"]]
        gate.set()
        await settle(engine)

    asyncio.run(scenario())


def test_missing_permission_stops_before_approval_and_says_how_to_fix(tmp_path):
    rec = Recorder()
    needs_ax = FakeAction("wa", ONE_WAY, rec)
    needs_ax.requires = ("accessibility",)

    async def denied():
        return {"accessibility": False, "screen": True}

    engine = Engine(MissionStore(tmp_path / "m.db"), catalog={"wa": needs_ax}, plan_fn=make_plan_fn(("wa", ONE_WAY, {})), permissions_fn=denied)

    async def scenario():
        mission = engine.create("say hi to Ridhi on WhatsApp")
        await settle(engine)
        result = engine.get(mission["id"])
        assert result["status"] == "failed" and result["needs"] == ["accessibility"]
        assert "Accessibility" in result["error"] and [s["status"] for s in result["steps"]] == ["skipped"]
        assert rec.calls == []

    asyncio.run(scenario())


def test_punctuation_only_input_is_not_planned(tmp_path):
    engine = build(tmp_path, {}, make_plan_fn())
    with pytest.raises(ValueError, match="didn't catch that"):
        engine.create("\\")
