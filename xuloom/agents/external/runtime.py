"""Shared external worker binding and checkpoints, using the native execution record."""
import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import artifact_directory, atomic_text
from .program import resolve, verify


class ExternalExecutionError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def tool_result_digest(content, is_error):
    """Match owned MCP receipts to external history without storing a second result."""
    normalized = []
    for block in content:
        if block.get("type") == "text":
            normalized.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            normalized.append({"type": "image", "data": block["data"], "mimeType": block["mimeType"]})
        else:
            raise ValueError("External history contains unsupported tool content")
    return hashlib.sha256(json.dumps([normalized, bool(is_error)], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def recovery_status(state):
    if not state: return None
    confirmed = state.get("phase") == "idle" and state.get("has_session") is True
    return {"resumable": confirmed, "reason": "confirmed_checkpoint" if confirmed else state.get("stop_error", "external_outcome_unknown")}


def configure(engine, definition):
    backend = definition.get("backend", "native")
    if backend not in {"native", "claude", "pi"}:
        raise ValueError("Unsupported external agent backend: " + str(backend))
    if backend == "native":
        engine.tool_context.pop("external_backend", None)
        return
    providers = {"anthropic", "openai"} if backend == "pi" else {"anthropic"}
    if engine.model_provider not in providers:
        raise ValueError("The external backend does not support this configured model protocol")
    if engine.execution_sandbox.enabled:
        raise ValueError("External CLI execution is not supported by this system isolation backend; no host fallback was started")
    binding = resolve(definition.get("backend_program"), backend=backend, workspace=engine.session_cwd)
    if definition.get("_external_binding") is not None and definition["_external_binding"] != binding:
        raise ValueError("External program changed while the worker was queued; no process was started")
    previous = engine.tool_context.get("external_agent")
    if previous and previous.get("binding") != binding:
        raise ValueError("The installed external backend changed; create a new worker after reviewing the program")
    engine.tool_context["external_backend"] = binding


def restore(engine, metadata):
    state = copy.deepcopy(metadata.get("external_agent"))
    if state is not None:
        if not isinstance(state, dict) or state.get("backend") not in {"claude", "pi"} or state.get("phase") not in {"idle", "running", "interrupted"}:
            raise ValueError("Invalid external execution checkpoint")
        if str(uuid.UUID(state["session_id"])) != state["session_id"] or not isinstance(state.get("seen_tool_ids"), list):
            raise ValueError("Invalid external session identity")
        if type(state.get("message_cursor")) is not int or state["message_cursor"] < 0:
            raise ValueError("Invalid external message checkpoint")
        if any(key in state for key in ("history_sha256", "history_offset", "entry_cursor")):
            digest = state.get("history_sha256", "")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or type(state.get("history_offset")) is not int or state["history_offset"] <= 0 or not isinstance(state.get("entry_cursor"), str) or not state["entry_cursor"]:
                raise ValueError("Invalid external history checkpoint")
        engine.tool_context["external_agent"] = state
    else:
        engine.tool_context.pop("external_agent", None)


def response(text, usage):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
        usage=SimpleNamespace(**usage))


async def prepare(engine, *, system_prompt, api_messages, model_name):
    from xuloom.core.model_inputs import validate_media, validate_transport
    validate_media(api_messages, engine._input_contract(model_name))
    validate_transport(api_messages, engine.model_provider)
    binding = engine.tool_context["external_backend"]
    try:
        await verify(binding, workspace=engine.session_cwd)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise ExternalExecutionError("external_dependency_error", "External dependency check failed: " + detail + "; no model was started") from error
    state = engine.tool_context.get("external_agent")
    if state is None:
        state = {"backend": binding["backend"], "binding": binding, "session_id": str(uuid.uuid4()), "phase": "idle",
            "model": model_name, "seen_tool_ids": [], "message_cursor": 0, "has_session": False}
        engine.tool_context["external_agent"] = state
    if state["backend"] != binding["backend"] or state["binding"] != binding or state["model"] != model_name:
        raise ExternalExecutionError("binding_changed", "External worker model/program changed; no request was started")
    if state["phase"] != "idle":
        raise ExternalExecutionError("external_outcome_unknown", "The external execution was interrupted. Inspect its saved transcript and tool receipts before starting a new worker; it was not automatically replayed.")
    inputs = [copy.deepcopy(m["content"]) for m in engine.messages[state["message_cursor"]:]
        if m.get("role") == "user" and not m.get("_is_tool_result")]
    if not inputs:
        if "result" in state:
            return {"cached": response(state["result"], {name: 0 for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")})}
        raise ExternalExecutionError("protocol_error", "External execution has no owned input")
    content = []
    for value in inputs:
        content.extend(value if isinstance(value, list) else [{"type": "text", "text": value}])
    owner = (engine.tool_context.get("delegation") or {}).get("owner_session_id") or engine.session_id
    home = artifact_directory(engine.session_cwd) / owner / "external" / state["session_id"]
    home.mkdir(parents=True, exist_ok=True)
    atomic_text(home / "instructions.txt", system_prompt)
    state.pop("result", None)
    state.pop("stop_checkpoint", None)
    state.pop("stop_error", None)
    state.update(phase="running", message_cursor=len(engine.messages))
    return {"state": state, "home": home, "content": content}


async def cleanup(engine, state, process, tasks, files):
    if process is not None and process.returncode is None:
        process.kill(); await process.wait()  # Only this task's owned child.
    for task in tasks:
        if not task.done(): task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for path in files: path.unlink(missing_ok=True)
    if state["phase"] == "running":
        state["phase"] = "interrupted"
        if not engine.persist_session_state():
            raise PersistenceError("External cancellation/failure could not be checkpointed; inspect saved execution before retrying")


async def sample(engine, **kwargs):
    if engine.tool_context["external_backend"]["backend"] == "pi":
        from .pi import sample as execute
    else:
        from .claude import sample as execute
    return await execute(engine, **kwargs)

