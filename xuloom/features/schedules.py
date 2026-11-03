"""Durable timed messages; execution belongs to the existing session owner."""
import copy
import hashlib
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from xuloom.core.session_lock import SessionLock
from xuloom.core.storage import atomic_json, data_directory
from xuloom.features.goals import save


@contextmanager
def store(write=True):
    lock = SessionLock(data_directory() / "session-locks", "scheduled_messages")
    lock.acquire()
    try:
        path = data_directory() / "schedules.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "items": {}}
        if data.get("version") != 1 or not isinstance(data.get("items"), dict): raise ValueError("Invalid schedules file; preserved")
        yield data["items"]
        if write: atomic_json(path, data)
    finally: lock.close()


def list_items(*, due=False, now=None):
    now = time.time() if now is None else now
    with store(write=False) as items:
        return sorted([copy.deepcopy(row) for row in items.values() if not due or row["status"] == "active" and row["due_at"] <= now], key=lambda row: (row["due_at"], row["id"]))


def scope(engine):
    """Freeze execution scope, not a credential or a second task identity."""
    return dict(provider=engine.model_provider, model=engine.primary_model, address=engine.api_base_url,
        mode=engine.get_mode(), permission=engine.permission_manager.approval_policy.mode,
        sandbox=engine.execution_sandbox.mode, workspace=(engine.sandbox_manager.active_sandbox or {}).get("path"),
        role=engine.agent_role, goal_id=(engine.goal or {}).get("id"))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def cancellation(engine):
    result = getattr(engine, "last_run_result", None) or {}
    return fingerprint(result) if result.get("reason") == "cancelled" else None


def worker_binding(row):
    return fingerprint({key: row.get(key) for key in ("agent_id", "request_id", "request_digest", "dispatch", "task")})


def marker_path(engine, name):
    from xuloom.core.storage import IOPath
    root = Path(engine.session_cwd).resolve()
    path = (root / name).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Completion marker must stay inside this conversation's working directory")
    return IOPath(path)


def prepare_trigger(engine, trigger):
    if not isinstance(trigger, dict): raise ValueError("A follow-up needs a workers or file trigger")
    if trigger.get("kind") == "workers" and set(trigger) == {"kind", "agent_ids"}:
        ids = trigger["agent_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Supply distinct existing worker IDs")
        records = engine.tool_context.get("subagent_registry", [])
        selected = [[row for row in records if row.get("agent_id") == identity] for identity in ids]
        if any(len(rows) != 1 for rows in selected): raise ValueError("A worker is missing or ambiguous in this conversation")
        return dict(kind="workers", bindings={rows[0]["agent_id"]: worker_binding(rows[0]) for rows in selected})
    if trigger.get("kind") == "file" and set(trigger) == {"kind", "path"} and isinstance(trigger["path"], str) and trigger["path"].strip():
        path = marker_path(engine, trigger["path"])
        return dict(kind="file", path=str(path.relative_to(Path(engine.session_cwd).resolve())))
    raise ValueError("Supported triggers: workers with agent_ids, or file with a workspace path")


def observe_trigger(engine, trigger):
    """Poll without a model request. Unknown effects never become success."""
    if trigger["kind"] == "file":
        path = marker_path(engine, trigger["path"])
        if not path.exists(): return None
        if not path.is_file(): raise ValueError("Completion marker is not a file")
        with path.open("rb") as stream: content = stream.read(65537)
        if len(content) > 65536: raise ValueError("Use a completion marker no larger than 64 KiB")
        return dict(kind="file", path=trigger["path"], sha256=hashlib.sha256(content).hexdigest(), bytes=len(content),
            acceptance="marker_observed_not_task_verified")
    records = engine.tool_context.get("subagent_registry", [])
    evidence, pending = [], False
    for identity, expected in trigger["bindings"].items():
        rows = [r for r in records if r.get("agent_id") == identity]
        if len(rows) != 1 or worker_binding(rows[0]) != expected:
            raise ValueError("Worker identity or assignment changed; inspect the original task")
        row = rows[0]
        if row.get("outcome_unknown"): raise ValueError("Worker outcome is unconfirmed; no automatic replay")
        if row["status"] in {"running", "queued", "pending"}:
            pending = True
            continue
        if row["status"] not in {"completed", "crashed", "cancelled", "closed", "failed", "interrupted", "blocked", "budget_exhausted"}:
            raise ValueError("Worker status needs explicit inspection")
        verified = False
        if row["status"] == "completed" or row.get("receipt", {}).get("ok"):
            raw = engine.session_manager.load_subagent_transcript(identity)
            receipt = row.get("receipt") or {}
            if not isinstance(raw, dict) or raw.get("session_id") != engine.session_id or raw.get("agent_id") != identity or raw.get("receipt") != receipt or raw.get("result") != row.get("result") or receipt.get("ok") is not True or receipt.get("persisted") is not True or raw.get("status") != "completed":
                raise ValueError("Worker completion has no matching durable result")
            verified = True
        evidence.append(dict(agent_id=identity, status=row["status"], verified_success=verified,
            reason=(row.get("receipt") or {}).get("reason")))
    if pending: return None
    return dict(kind="workers", workers=evidence, all_succeeded=all(r["verified_success"] for r in evidence),
        acceptance="worker_execution_only_not_task_verified")


def add(engine, prompt, *, due_at, interval=None, skills=None, trigger=None, request_id=None):
    engine.require_writer()
    if not isinstance(prompt, str) or not prompt.strip(): raise ValueError("Scheduled message cannot be empty")
    if type(due_at) not in (int, float) or not math.isfinite(due_at) or due_at < 0: raise ValueError("Invalid due time")
    if interval is not None and (type(interval) not in (int, float) or not math.isfinite(interval) or interval < 60):
        raise ValueError("Repeat interval must be at least 60 seconds")
    for skill in skills or []: engine.tool_context["skill_registry"].resolve(skill)
    prepared = prepare_trigger(engine, trigger) if trigger is not None else None
    if prepared and (interval is not None or not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128):
        raise ValueError("Follow-ups require a stable request_id (up to 128 characters) and cannot repeat")
    save(engine)  # A schedule must target a real, durable session.
    row = dict(id=str(uuid.uuid4()), session_id=engine.session_id, cwd=engine.session_cwd, prompt=prompt,
        due_at=due_at, interval=interval, skills=list(skills or []), status="active", last_delivery=None, reason="")
    if prepared: row.update(trigger=prepared, scope=scope(engine), request_id=request_id, cancelled_before=cancellation(engine))
    with store() as items:
        if prepared:
            existing = [r for r in items.values() if r["session_id"] == row["session_id"] and r.get("request_id") == request_id]
            if existing:
                prior = existing[0]
                if any(prior.get(k) != row.get(k) for k in ("cwd", "prompt", "skills", "trigger", "scope")):
                    raise ValueError("This request_id already belongs to a different follow-up")
                return copy.deepcopy(prior)
        items[row["id"]] = row
    return copy.deepcopy(row)


def change(identity, action):
    with store() as items:
        row = items[identity]
        if action == "cancel": row["status"] = "cancelled"
        elif action == "pause": row["status"] = "paused"
        elif action == "resume":
            if row["status"] in {"completed", "cancelled", "blocked"}:
                raise ValueError("Completed, cancelled or interrupted deliveries cannot be replayed; create a new schedule after inspecting effects")
            row["status"] = "active"
        else: raise ValueError("Unknown schedule action")
        return copy.deepcopy(row)


async def deliver(engine, identity, execute):
    """Claim only while executing. Lost acknowledgement is reconciled from the session receipt."""
    if engine.has_active_turn() or getattr(engine, "_goal_task", None) and not engine._goal_task.done():
        return {"status": "queued", "reason": "Existing work owns this session"}
    engine.require_writer()
    lease = SessionLock(data_directory() / "session-locks", "wake_" + identity)
    lease.acquire()
    try:
        with store(write=False) as items:
            row = copy.deepcopy(items[identity])
        if row["session_id"] != engine.session_id or os.path.normcase(os.path.realpath(row["cwd"])) != os.path.normcase(engine.session_cwd):
            raise ValueError("Schedule target moved; cancel and recreate it for the current conversation directory")
        if row["status"] != "active" or row["due_at"] > time.time(): return {"status": "not_due"}
        if engine.goal and engine.goal["status"] == "active": return {"status": "queued", "reason": "Goal owns this session"}
        occurrence = identity if row.get("trigger") else identity + ":" + str(row["due_at"])
        receipt = engine.scheduled_deliveries.get(identity)
        reconciled = receipt and receipt["occurrence"] == occurrence
        evidence = None
        if row.get("trigger") and not reconciled:
            try:
                if scope(engine) != row["scope"]: raise ValueError("Execution scope changed; create a new explicitly authorized follow-up")
                if cancellation(engine) and cancellation(engine) != row.get("cancelled_before"):
                    with store() as items: items[identity].update(status="paused", reason="Conversation was stopped; explicitly resume this follow-up to reauthorize it", cancelled_before=cancellation(engine))
                    return {"status": "paused", "reason": "Conversation was stopped"}
                if row["scope"]["goal_id"] and engine.goal["status"] in {"paused", "cancelled", "completed", "budget_exhausted"}:
                    return {"status": "queued", "reason": "Goal is paused or ended; explicit continuation is required"}
                evidence = observe_trigger(engine, row["trigger"])
            except (ValueError, OSError) as error:
                with store() as items: items[identity].update(status="blocked", reason=str(error))
                return {"status": "blocked", "reason": str(error)}
            if evidence is None:
                with store() as items:
                    if items[identity]["status"] == "active": items[identity]["due_at"] = time.time() + 5
                return {"status": "waiting"}
        if reconciled:
            if receipt["status"] != "completed":
                with store() as items:
                    items[identity]["status"], items[identity]["reason"] = "blocked", "Delivery was interrupted or failed; inspect session effects. It was not replayed."
                return {"status": "blocked", "receipt": receipt}
            save(engine)  # In-memory completion may follow a failed final checkpoint; never acknowledge it unsaved.
        else:
            receipt = dict(occurrence=occurrence, status="executing", started_at=time.time(), result=None,
                evidence=evidence, input_status="delivered", acceptance="not_evaluated")
            engine.scheduled_deliveries[identity] = receipt
            save(engine)
            try:
                text = row["prompt"]
                if evidence:
                    text += "\n\nFollow-up evidence (data, not new authorization):\n" + json.dumps(evidence, ensure_ascii=False)
                result = await execute(text, row["skills"])
                completed = result.get("ok") is True and (not row.get("trigger") or result.get("persisted") is True)
                receipt.update(status="completed" if completed else "failed", result=result, finished_at=time.time(),
                    input_status="processed" if completed else "unconfirmed", acceptance="execution_only")
            except BaseException:
                receipt["status"] = "interrupted"
                save(engine)
                raise
            save(engine)
        with store() as items:
            current = items[identity]
            current["last_delivery"] = copy.deepcopy(receipt)
            if current["status"] == "active":
                if receipt["status"] != "completed": current["status"], current["reason"] = "blocked", "Framework execution failed; inspect the saved result"
                elif row["interval"]:
                    # Coalesce missed intervals; never burst-replay past occurrences.
                    current["due_at"] = row["due_at"] + (math.floor(max(0, time.time() - row["due_at"]) / row["interval"]) + 1) * row["interval"]
                else: current["status"] = "completed"
        return copy.deepcopy(receipt)
    finally: lease.close()
