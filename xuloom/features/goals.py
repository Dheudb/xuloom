"""Optional goal policy over the existing engine, receipts and session checkpoint."""
import asyncio
import copy
import hashlib
import json
import math
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import artifact_directory, atomic_json
from xuloom.core.session_lock import SessionLock

TERMINAL = {"completed", "cancelled", "budget_exhausted"}
CURRENT_BUDGET = ContextVar("xuloom_goal_budget", default=None)


def request_control(cwd, session_id, goal_id, action):
    if action not in {"pause", "cancel"}: raise ValueError("Only pause/cancel can be queued for a live owner")
    lock = SessionLock(artifact_directory(cwd).parent / "goal-controls", session_id)
    lock.acquire()
    try:
        atomic_json(artifact_directory(cwd) / session_id / "goal-control.json", dict(goal_id=goal_id, action=action))
    finally: lock.close()


async def pending_control(engine):
    path = artifact_directory(engine.session_cwd) / engine.session_id / "goal-control.json"
    if not path.exists(): return
    lock = SessionLock(artifact_directory(engine.session_cwd).parent / "goal-controls", engine.session_id)
    lock.acquire()
    try:
        if not path.exists(): return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["goal_id"] == engine.goal["id"] and payload["action"] in {"pause", "cancel"}:
            # Save requested state before acknowledging the mailbox. Repeated pause/cancel is harmless.
            engine.goal["status"] = "paused" if payload["action"] == "pause" else "cancelled"
            engine.goal["reason"] = "Requested by host control mailbox"
            save(engine)
        path.unlink()
    finally: lock.close()
    if engine.goal["status"] != "active": await engine.request_abort()


def save(engine):
    if not engine.persist_session_state():
        raise PersistenceError("Goal checkpoint could not be saved; execution stopped.")


def create(engine, objective, criteria, *, checks=None, rounds=20, seconds=1800, output_tokens=50000, skills=None, continuous=False, limits=None):
    engine.require_writer()
    if engine.goal and engine.goal["status"] not in TERMINAL:
        raise ValueError("Finish or cancel the existing goal first")
    if not isinstance(objective, str) or not objective.strip() or not isinstance(criteria, list) or not criteria or any(not isinstance(c, str) or not c.strip() for c in criteria):
        raise ValueError("A goal needs an objective and nonempty acceptance criteria")
    for value, limit in ((rounds, 10000), (seconds, 604800), (output_tokens, 10000000)):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= limit:
            raise ValueError("Goal budgets must be finite, positive and within supported limits")
    if type(rounds) is not int or type(output_tokens) is not int:
        raise ValueError("Round and output-token budgets must be integers")
    if limits is not None:
        ceilings = dict(rounds=10000, seconds=604800, output_tokens=10000000)
        if not continuous or not isinstance(limits, dict) or limits.keys() - ceilings.keys():
            raise ValueError("Optional limits require a continuous goal and known budget fields")
        for key, value in limits.items():
            if type(value) is not int or not 0 < value <= ceilings[key]:
                raise ValueError("Optional goal limits must be positive whole numbers within supported bounds")
    checks = checks or []
    from .acceptance import validate
    for check in checks:
        validate(check)
        check_path(engine, check["path"])
    for name in skills or []:
        engine.tool_context["skill_registry"].resolve(name)
    goal = dict(version=1, id=str(uuid.uuid4()), objective=objective.strip(), criteria=criteria,
        checks=copy.deepcopy(checks), skills=list(skills or []), budget=dict(rounds=rounds, seconds=seconds, output_tokens=output_tokens),
        rounds=0, elapsed_seconds=0, output_used=0, output_reserved=0, stalls=0, status="paused", phase="ready",
        reason="Created; explicitly run or resume to start", report=None, last_progress=None, last_result=None, continuous=continuous)
    if continuous:
        goal["budget"] = {**dict(rounds=None, seconds=None, output_tokens=None), **(limits or {})}
    previous, engine.goal = engine.goal, goal
    try: save(engine)
    except BaseException:
        engine.goal = previous
        raise
    return copy.deepcopy(goal)


def check_path(engine, name):
    root = Path(engine.session_cwd).resolve()
    path = (root / name).resolve()
    if path != root and root not in path.parents:
        raise ValueError("Acceptance check paths must stay in the conversation working directory")
    return path


def recover(goal):
    goal = copy.deepcopy(goal)
    if goal and (goal.get("status") == "active" or goal.get("phase") == "executing"):
        goal["status"], goal["phase"] = "interrupted", "ready"
        goal["reason"] = "Previous owner exited; inspect saved results and explicitly resume. No work was replayed."
        goal["output_used"] += goal.get("output_reserved", 0)
        goal["output_reserved"] = 0
        goal["elapsed_seconds"] += min(goal["budget"]["seconds"] or float("inf"), max(0, time.time() - goal.get("round_started_at", time.time())))
    return goal


def snapshot(manager, session_id, goal):
    """Reading a live owner's checkpoint must not falsely report a crash."""
    from xuloom.core.session_lock import SessionBusyError
    if not goal: return None
    if manager.owns_writer(session_id): return copy.deepcopy(goal)
    probe = SessionLock(manager.lock_directory, session_id)
    try: probe.acquire()
    except SessionBusyError: return {**copy.deepcopy(goal), "owner_busy": True}
    else:
        probe.close()
        return {**recover(goal), "owner_busy": False}


async def control(engine, action, reason=""):
    engine.require_writer()
    goal = engine.goal
    if not goal: raise ValueError("This session has no goal")
    if action not in {"pause", "cancel", "resume", "accept"}: raise ValueError("Unknown goal action")
    if goal["status"] in TERMINAL: raise ValueError("Goal is terminal; create a new goal instead")
    if action == "accept":
        if not reason.strip(): raise ValueError("Explicit acceptance needs a reason")
        if getattr(engine, "_goal_task", None) and not engine._goal_task.done(): raise ValueError("Pause the goal before accepting")
        goal["status"], goal["accepted_by"] = "completed", "user_or_host"
    else: goal["status"] = {"pause": "paused", "cancel": "cancelled", "resume": "active"}[action]
    goal["reason"] = reason or action
    save(engine)
    if action in {"pause", "cancel"}: await engine.request_abort()
    return copy.deepcopy(goal)


def available_output(goal):
    if goal["budget"]["output_tokens"] is None: return float("inf")
    return max(0, goal["budget"]["output_tokens"] - goal["output_used"] - goal["output_reserved"])


def update_objective(engine, objective):
    engine.require_writer()
    if not isinstance(objective, str) or not objective.strip(): raise ValueError("请填写目标")
    if engine.has_active_turn() or getattr(engine, "_goal_task", None): raise ValueError("Pause the goal before editing")
    if not engine.goal or engine.goal["status"] == "cancelled": raise ValueError("No editable goal")
    previous = copy.deepcopy(engine.goal)
    engine.goal.update(objective=objective.strip(), criteria=[objective.strip()], checks=[], continuous=True,
        status="paused", phase="ready", report=None,
        previous_report=None, last_progress=None, stalls=0, reason="目标已修改，等待继续")
    engine.goal.pop("accepted_by", None)
    try: save(engine)
    except BaseException:
        engine.goal = previous
        raise
    return copy.deepcopy(engine.goal)


async def run(engine, emit, validator=None):
    """Explicit host entry. Automatic continuation never creates a new execution engine."""
    if engine.has_active_turn() or getattr(engine, "_goal_task", None) and not engine._goal_task.done():
        raise ValueError("Session already has running work")
    engine.require_writer()
    goal = engine.goal
    if not goal or goal["status"] in TERMINAL: raise ValueError("No resumable goal")
    engine._goal_task = asyncio.current_task()
    engine.tool_context["goal_budget"] = goal
    budget_context = CURRENT_BUDGET.set(goal)
    engine.activate_tools(["goal_report_tool"])
    goal["status"], goal["reason"] = "active", "Explicitly resumed"
    try:
        while goal["status"] == "active":
            await pending_control(engine)
            if goal["status"] != "active": break
            if getattr(engine, "_noninteractive_approval_required", False) or getattr(engine, "_noninteractive_question_required", False):
                goal["status"], goal["reason"] = "blocked", "User input or approval required; non-interactive execution cannot grant it"
                break
            remaining = float("inf") if goal["budget"]["seconds"] is None else goal["budget"]["seconds"] - goal["elapsed_seconds"]
            if (goal["budget"]["rounds"] is not None and goal["rounds"] >= goal["budget"]["rounds"]) or remaining <= 0 or available_output(goal) <= 0:
                goal["status"], goal["reason"] = "budget_exhausted", "Goal round, active-time or output-token budget exhausted"
                break
            goal["rounds"] += 1
            goal["phase"], goal["report"] = "executing", None
            goal["round_started_at"] = time.time()
            save(engine)
            await emit({"type": "goal_changed", "goal": copy.deepcopy(goal)})
            prompt = ("Continue the explicitly authorized goal using saved evidence; do not repeat uncertain operations.\n"
                + json.dumps({"objective": goal["objective"], "acceptance": goal["criteria"], "round": goal["rounds"],
                    "last_report": goal.get("previous_report"), "budget": goal["budget"]}, ensure_ascii=False)
                + "\nUse goal_report_tool to report progress, evidence for each acceptance criterion, completion or a concrete blocker. A completion claim alone is not verified acceptance."
                + (" Continue until the objective is fulfilled, a declared budget is exhausted or a concrete blocker requires user input. Null budget fields mean no limit. Report complete when done; this is a model report, not independent scientific validation." if goal.get("continuous") else ""))
            result, started = None, time.monotonic()
            async def round_turn():
                nonlocal result
                engine._goal_turn_task = asyncio.current_task()
                async for event in engine.submit_message(prompt, display_text=goal["objective"] if goal["rounds"] == 1 else "继续目标：" + goal["objective"], selected_skills=goal["skills"], sys_print_callback=lambda _: None):
                    if event["type"] == "final":
                        result = event["result"]
                        await emit({"type": "goal_round_result", "result": result})
                    else: await emit(event)
            try:
                task = asyncio.create_task(round_turn())
                deadline = time.monotonic() + remaining
                while not task.done():
                    await pending_control(engine)
                    if goal["status"] != "active":
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        break
                    if time.monotonic() >= deadline:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise asyncio.TimeoutError()
                    await asyncio.wait({task}, timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                if not task.cancelled(): await task
            except asyncio.TimeoutError:
                await engine.request_abort()
                goal["status"], goal["reason"] = "budget_exhausted", "Active-time budget exhausted"
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await engine.request_abort()
                if goal["status"] == "active": goal["status"], goal["reason"] = "interrupted", "Execution interrupted; explicitly resume after inspecting state"
                raise
            finally:
                engine._goal_turn_task = None
                goal["elapsed_seconds"] += time.monotonic() - started
                goal["phase"] = "ready"
                goal["last_result"] = result
                save(engine)
            if goal["status"] != "active": break
            if getattr(engine, "_noninteractive_approval_required", False) or getattr(engine, "_noninteractive_question_required", False):
                goal["status"], goal["reason"] = "blocked", "User input or approval required; non-interactive execution cannot grant it"
                break
            if not result or not result.get("ok"):
                if goal.get("continuous") and result and result.get("persisted") and result.get("reason") == "turn_limit":
                    continue
                goal["status"], goal["reason"] = "blocked", "Framework execution did not complete: " + str((result or {}).get("reason", "missing_result"))
                break
            report = goal.get("report") or {}
            goal["previous_report"] = report
            checks = []
            from .acceptance import evaluate
            for check in goal["checks"]:
                path = check_path(engine, check["path"])
                passed = evaluate(path,check)
                checks.append({**check, "passed": passed})
            goal["check_results"] = checks
            if report.get("status") == "blocked":
                goal["status"], goal["reason"] = "blocked", report["summary"]
            elif report.get("status") == "complete":
                host_accepted = await validator(copy.deepcopy(goal)) if validator else False
                accepted = (bool(host_accepted) if validator else bool(checks)) and all(c["passed"] for c in checks)
                if goal["status"] != "active": break
                if accepted:
                    goal["status"], goal["reason"], goal["accepted_by"] = "completed", "Acceptance checks passed", "host_validator" if validator else "declared_file_checks"
                elif not checks and not validator:
                    if goal.get("continuous"):
                        goal["status"], goal["reason"], goal["accepted_by"] = "completed", report["summary"], "model_report"
                    else:
                        goal["status"], goal["reason"] = "awaiting_review", "Completion reported; explicit user/host acceptance is required for these textual criteria"
            progress = hashlib.sha256(json.dumps({"tools": sorted({(r.get("tool_name"), r.get("input_digest"), json.dumps(r.get("tool_result"), sort_keys=True, default=str)) for r in engine.recent_tool_activity if r.get("status") == "ok" and r.get("tool_name") != "goal_report_tool"}),
                "files": engine.tool_context.get("file_changes", []), "workers": engine.subagent_registry}, sort_keys=True, default=str).encode()).hexdigest()
            goal["stalls"] = goal["stalls"] + 1 if progress == goal.get("last_progress") else 1
            if progress != goal.get("last_progress"): goal["stalls"] = 0 if any(r.get("tool_name") != "goal_report_tool" for r in engine.recent_tool_activity) else 1
            goal["last_progress"] = progress
            if not goal.get("continuous") and goal["status"] == "active" and goal["stalls"] >= 3:
                goal["status"], goal["reason"] = "blocked", "Three rounds without new tool, artifact or worker evidence; no automatic retry"
            save(engine)
        save(engine)
        await emit({"type": "goal_final", "goal": copy.deepcopy(goal), "result": goal.get("last_result")})
        return copy.deepcopy(goal)
    except Exception as error:
        goal["status"], goal["reason"] = "blocked", str(error)
        try: save(engine)
        except Exception: pass  # The original checkpoint and original error remain authoritative.
        raise
    finally:
        CURRENT_BUDGET.reset(budget_context)
        engine.tool_context.pop("goal_budget", None)
        engine._goal_task = None
