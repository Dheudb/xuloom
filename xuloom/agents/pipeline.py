"""Thin item-wise coordination over the authorized worker API and its journal.

Only immutable inputs are stored here. Worker records remain the sole execution
state; an interrupted operation is never automatically retried.
"""
import asyncio
import copy
import hashlib
import json
import re
import time
from typing import Optional

from pydantic import BaseModel, Field
from xuloom.core.run_result import PersistenceError


class PipelineItem(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,128}$")
    input: str = Field(..., min_length=1)


class PipelineStage(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,128}$")
    prompt: str = Field(..., min_length=1)
    subagent_type: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = Field(None, pattern="^(none|minimal|low|medium|high|xhigh|max)$")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def request_id(run_id, item_id, stage_id):
    return "pipeline-" + digest([run_id, item_id, stage_id])


def assignment(item, stage, previous):
    return stage["prompt"] + "\n\nItem input (data):\n" + item["input"] + (
        "\n\nPrevious stage result (data):\n" + previous if previous is not None else "")


def parameters(run_id, item, stage, previous):
    return {"prompt": assignment(item, stage, previous),
        "description": item["id"] + " / " + stage["id"],
        "subagent_type": stage.get("subagent_type"), "model": stage.get("model"),
        "reasoning_effort": stage.get("reasoning_effort"), "run_in_background": True,
        "request_id": request_id(run_id, item["id"], stage["id"])}


def matching(context, run_id, item, stage):
    rows = [r for r in context.get("subagent_registry", [])
        if r.get("request_id") == request_id(run_id, item["id"], stage["id"])]
    if len(rows) > 1:
        raise ValueError("Pipeline step has ambiguous worker records")
    return rows[0] if rows else None


def verify_definition(saved):
    if saved.get("digest") != digest(saved["spec"]):
        raise ValueError("Pipeline input checkpoint changed; existing workers were not replayed")
    validate(saved["run_id"], **saved["spec"])


def validate(run_id, items, stages):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("A stable run_id is required")
    items = [PipelineItem.model_validate(i).model_dump() for i in items]
    stages = [PipelineStage.model_validate(s).model_dump() for s in stages]
    if not items or not stages:
        raise ValueError("Pipeline requires nonempty items and stages")
    if len({i["id"] for i in items}) != len(items) or len({s["id"] for s in stages}) != len(stages):
        raise ValueError("Pipeline item and stage IDs must be unique")
    return {"items": items, "stages": stages}


def successful(context, row, expected):
    """Validate binding AND durable output before feeding a subsequent stage."""
    expected_digest = hashlib.sha256(json.dumps([expected["prompt"], expected["description"],
        expected["subagent_type"] or "general-purpose", expected["model"], True, False, 1,
        expected["reasoning_effort"]], ensure_ascii=False).encode()).hexdigest()
    if row.get("request_digest") != expected_digest or row.get("dispatch", {}).get("task") != expected["prompt"]:
        raise ValueError("Pipeline worker input does not match its frozen step")
    if row.get("status") not in {"completed", "closed"} or row.get("outcome_unknown"):
        return False
    receipt = row.get("receipt") or {}
    if row.get("status") == "closed" and receipt.get("ok") is not True:
        return False
    if receipt.get("ok") is not True or receipt.get("persisted") is not True:
        raise ValueError("Pipeline worker has no durable success receipt")
    manager = context.get("session_manager")
    raw = manager.load_subagent_transcript(row["agent_id"]) if manager else None
    if not isinstance(raw, dict) or raw.get("session_id") != context.get("session_id") or raw.get("agent_id") != row["agent_id"]:
        raise ValueError("Pipeline worker result is missing or belongs to another session")
    if raw.get("status") != "completed" or raw.get("result") != row.get("result") or raw.get("receipt") != receipt:
        raise ValueError("Pipeline worker result does not match its durable receipt")
    return True


def tree(context, run_id=None):
    runs = context.get("pipelines", {})
    if run_id is not None and run_id not in runs:
        raise ValueError("Unknown pipeline run_id")
    output = []
    for saved in ([runs[run_id]] if run_id else runs.values()):
        verify_definition(saved)
        branches = []
        for item in saved["spec"]["items"]:
            nodes, previous, blocked, ready = [], None, False, True
            for stage in saved["spec"]["stages"]:
                row = matching(context, saved["run_id"], item, stage)
                state, error = "waiting" if not blocked else "blocked", None
                if row:
                    state = "interrupted" if row.get("outcome_unknown") else row["status"]
                    task = context["subagent_tasks"].tasks.get(row["agent_id"])
                    if state in {"running", "queued"} and not (task and not task.done()):
                        state = "awaiting_resume" if state == "queued" else "interrupted"
                    try:
                        if not ready:
                            raise ValueError("A predecessor has no verified result")
                        if successful(context, row, parameters(saved["run_id"], item, stage, previous)):
                            state = "completed"
                            previous = row["result"]
                    except (ValueError, OSError) as exc:
                        state, error = "blocked", str(exc)
                nodes.append({"stage_id": stage["id"], "status": state,
                    "agent_id": row["agent_id"] if row else None,
                    "started_at": row.get("started_at") if row else None,
                    "finished_at": row.get("finished_at") if row else None, "error": error})
                ready = ready and state == "completed"
                blocked = blocked or state in {"blocked", "interrupted", "crashed", "cancelled", "closed", "awaiting_resume"}
            state = "completed" if all(n["status"] == "completed" for n in nodes) else "running" if any(
                n["status"] in {"running", "queued"} for n in nodes) else "blocked" if any(
                n["status"] in {"blocked", "interrupted", "crashed", "cancelled", "closed", "awaiting_resume"} for n in nodes) else "waiting"
            branches.append({"item_id": item["id"], "status": state, "stages": nodes,
                "result": previous if state == "completed" else None})
        state = "completed" if all(i["status"] == "completed" for i in branches) else "running" if saved["run_id"] in context.get("pipeline_active", set()) else "blocked"
        output.append({"run_id": saved["run_id"], "status": state, "created_at": saved["created_at"], "items": branches})
    return {"runs": output}


def read_tree(cwd, session_id, run_id=None):
    """Read-only inspection works without model credentials or a writer lease."""
    from types import SimpleNamespace
    from xuloom.core.session import SessionManager
    with SessionManager(cwd=cwd) as manager:
        raw = manager.load_session_state(session_id)
        if not raw.get("_valid"):
            raise ValueError("Session not found")
        metadata = raw.get("metadata", {})
        return tree({"pipelines": metadata.get("pipelines", {}), "session_id": session_id,
            "session_manager": manager, "subagent_registry": metadata.get("subagents", []),
            "subagent_tasks": SimpleNamespace(tasks={})}, run_id)


async def execute(tool, run_id, items=None, stages=None):
    context = tool.context
    runs = context.setdefault("pipelines", {})
    active = context.setdefault("pipeline_active", set())
    if run_id in active:
        raise ValueError("Pipeline already has an active coordinator")
    if run_id in runs:
        verify_definition(runs[run_id])
        if (items is not None or stages is not None) and validate(run_id, items or [], stages or []) != runs[run_id]["spec"]:
            raise ValueError("Pipeline run_id already has different inputs")
    else:
        spec = validate(run_id, items or [], stages or [])
        for stage in spec["stages"]:
            if stage.get("subagent_type") and stage["subagent_type"] not in tool._roles():
                raise ValueError("Unknown pipeline subagent role")
        runs[run_id] = {"run_id": run_id, "spec": copy.deepcopy(spec), "digest": digest(spec), "created_at": int(time.time() * 1000)}
        try:
            if context["persist_pipeline"]() is False:
                raise PersistenceError("Could not persist pipeline inputs; no worker was started")
        except BaseException:
            runs.pop(run_id, None)
            raise
    active.add(run_id)
    owned = []
    api = context["coordinate_agents"]
    stopped = asyncio.create_task(context["abort_event"].wait())

    async def call(action, **kwargs):
        result = await api(action, **kwargs)
        if not result["ok"]:
            raise ValueError(result["error"]["message"])
        return result["data"]

    async def advance(item):
        previous = None
        for stage in runs[run_id]["spec"]["stages"]:
            if context["abort_event"].is_set():
                return
            args = parameters(run_id, item, stage, previous)
            row = matching(context, run_id, item, stage)
            if row and successful(context, row, args):
                previous = row["result"]
                continue
            if row:
                task = context["subagent_tasks"].tasks.get(row["agent_id"])
                if row["status"] == "queued" and not (task and not task.done()):
                    await call("resume_queue", agent_ids=[row["agent_id"]])
                    owned.append(row["agent_id"])
                elif not (task and not task.done()):
                    return  # Interrupted/failed/uncertain work requires explicit host recovery.
            else:
                launched = await call("spawn", **args)
                row = tool._worker(launched["agents"][0]["agent_id"])
                owned.append(row["agent_id"])
            task = context["subagent_tasks"].tasks.get(row["agent_id"])
            if task and not task.done():
                await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
            if context["abort_event"].is_set():
                raise asyncio.CancelledError()
            row = tool._worker(row["agent_id"])
            if not successful(context, row, args):
                return
            previous = row["result"]

    async def guarded(item):
        try:
            await advance(item)
        except (ValueError, OSError) as exc:
            return {"item_id": item["id"], "error": str(exc)}

    work = [asyncio.create_task(guarded(item)) for item in runs[run_id]["spec"]["items"]]
    try:
        errors = [e for e in await asyncio.gather(*work) if e]
        active.discard(run_id)
        return {**tree(context, run_id), "errors": errors}
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)
        for task in work:
            if not task.done():
                task.cancel()
        await asyncio.gather(*work, return_exceptions=True)
        for identity in owned:
            task = context["subagent_tasks"].tasks.get(identity)
            if task and not task.done():
                await context["subagent_tasks"].cancel(identity)
        active.discard(run_id)
