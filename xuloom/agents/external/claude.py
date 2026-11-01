"""Optional installed Claude Code as a sampler, using the existing owned runtime."""
import asyncio
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import artifact_directory, atomic_text
from .host import LocalHost, LIMIT
from .program import environment, resolve, verify


class ExternalExecutionError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def configure(engine, definition):
    backend = definition.get("backend", "native")
    if backend not in {"native", "claude"}:
        raise ValueError("Unsupported external agent backend: " + str(backend))
    if backend == "native":
        engine.tool_context.pop("external_backend", None)
        return
    if engine.model_provider != "anthropic":
        raise ValueError("Claude Code requires an explicit Anthropic Messages model connection")
    if engine.execution_sandbox.enabled:
        raise ValueError("External Claude execution is not supported by this system isolation backend; no host fallback was started")
    binding = resolve(definition.get("backend_program"))
    if definition.get("_external_binding") is not None and definition["_external_binding"] != binding:
        raise ValueError("External program changed while the worker was queued; no process was started")
    previous = engine.tool_context.get("external_agent")
    if previous and previous.get("binding") != binding:
        raise ValueError("The installed external backend changed; create a new worker after reviewing the program")
    engine.tool_context["external_backend"] = binding


def restore(engine, metadata):
    state = copy.deepcopy(metadata.get("external_agent"))
    if state is not None:
        if not isinstance(state, dict) or state.get("backend") != "claude" or state.get("phase") not in {"idle", "running", "interrupted"}:
            raise ValueError("Invalid external execution checkpoint")
        if str(uuid.UUID(state["session_id"])) != state["session_id"] or not isinstance(state.get("seen_tool_ids"), list):
            raise ValueError("Invalid external session identity")
        if type(state.get("message_cursor")) is not int or state["message_cursor"] < 0:
            raise ValueError("Invalid external message checkpoint")
        engine.tool_context["external_agent"] = state
    else:
        engine.tool_context.pop("external_agent", None)


def response(text, usage):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
        usage=SimpleNamespace(**usage))


async def sample(engine, *, system_prompt, api_messages, turn_count, model_name, max_tokens,
                 event_callback=None, **_):
    from xuloom.core.engine import AbortRequestedError
    from xuloom.core.model_inputs import validate_media, validate_transport
    validate_media(api_messages, engine._input_contract(model_name))
    validate_transport(api_messages, engine.model_provider)
    binding = engine.tool_context["external_backend"]
    try:
        await verify(binding)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise ExternalExecutionError("external_dependency_error", "The installed Claude Code no longer satisfies its frozen execution contract; no model was started") from error
    state = engine.tool_context.get("external_agent")
    if state is None:
        state = {"backend": "claude", "binding": binding, "session_id": str(uuid.uuid4()), "phase": "idle",
            "model": model_name, "seen_tool_ids": [], "message_cursor": 0, "has_session": False}
        engine.tool_context["external_agent"] = state
    if state["binding"] != binding or state["model"] != model_name:
        raise ExternalExecutionError("binding_changed", "External worker model/program changed; no request was started")
    if state["phase"] != "idle":
        raise ExternalExecutionError("external_outcome_unknown", "The external execution was interrupted. Its transcript and tool receipts were preserved; inspect them before starting a new worker. It was not automatically replayed.")
    cursor = state["message_cursor"]
    inputs = [copy.deepcopy(m["content"]) for m in engine.messages[cursor:]
        if m.get("role") == "user" and not m.get("_is_tool_result")]
    if not inputs:
        if "result" in state:
            return response(state["result"], {name: 0 for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}), [], False
        raise ExternalExecutionError("protocol_error", "External execution has no owned input")
    content = []
    for value in inputs:
        content.extend(value if isinstance(value, list) else [{"type": "text", "text": value}])
    owner = (engine.tool_context.get("delegation") or {}).get("owner_session_id") or engine.session_id
    home = artifact_directory(engine.session_cwd) / owner / "external" / state["session_id"]
    home.mkdir(parents=True, exist_ok=True)
    atomic_text(home / "instructions.txt", system_prompt)
    config_path = home / "connection.json"
    state.update(phase="running", message_cursor=len(engine.messages))
    process = None
    tasks = []
    try:
        async with LocalHost(engine, state, event_callback=event_callback, turn=turn_count) as host:
            host.maximum = max_tokens
            await host.checkpoint()
            config = {"mcpServers": {"xuloom": {"type": "http", "url": host.url + "/mcp", "headers": {"Authorization": "Bearer " + host.token}}}}
            atomic_text(config_path, json.dumps(config))
            env = environment(home)
            env.update(ANTHROPIC_BASE_URL=host.url, ANTHROPIC_API_KEY=host.token,
                HTTP_PROXY=host.url, HTTPS_PROXY=host.url, NO_PROXY="127.0.0.1,localhost,::1")
            args = [binding["program"], "--print", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages", "--bare", "--restricted", "--tools", "",
                "--strict-mcp-config", "--mcp-config", str(config_path), "--setting-sources", "", "--model", model_name,
                "--system-prompt-file", str(home / "instructions.txt"), "--allowedTools", "mcp__xuloom__*", "--permission-prompts", "none",
                "--resume" if state["has_session"] else "--session-id", state["session_id"]]
            effort = engine.reasoning_effort
            if effort:
                if effort not in {"low", "medium", "high", "xhigh", "max"}:
                    raise ValueError("This external backend does not support the selected reasoning effort")
                args.extend(["--effort", effort])
            process = await asyncio.create_subprocess_exec(*args, cwd=engine.session_cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LIMIT,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            initialized, final = False, None
            async def read_output():
                nonlocal initialized, final
                with (home / (uuid.uuid4().hex + ".jsonl")).open("xb") as transcript:
                    while line := await process.stdout.readline():
                        transcript.write(line)
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ExternalExecutionError("protocol_error", "External program returned an invalid event")
                        if row.get("session_id") not in {None, state["session_id"]}:
                            raise ExternalExecutionError("protocol_error", "External program changed its owned session")
                        if row.get("type") == "system" and row.get("subtype") == "init":
                            if initialized or any(not name.startswith("mcp__xuloom__") and name not in {"ToolSearch", "EndConversation"} for name in row.get("tools", [])):
                                raise ExternalExecutionError("protocol_error", "External program exposed uncontrolled execution tools")
                            initialized = True
                        if row.get("type") == "stream_event":
                            event = row.get("event", {})
                            delta = event.get("delta", {})
                            if delta.get("type") in {"text_delta", "thinking_delta"}:
                                await engine._emit_stream_event(event_callback, {"type": "text_delta" if delta["type"] == "text_delta" else "thinking_delta",
                                    "turn": turn_count, "text": delta.get("text", delta.get("thinking", ""))})
                        if row.get("type") == "result":
                            if final is not None:
                                raise ExternalExecutionError("protocol_error", "External program returned duplicate completion receipts")
                            final = row
                    transcript.flush(); os.fsync(transcript.fileno())
            async def read_error():
                # Drain stderr without echoing provider bodies or environment to chat.
                with (home / (uuid.uuid4().hex + ".stderr")).open("xb") as transcript:
                    while chunk := await process.stderr.read(65536): transcript.write(chunk)
                    transcript.flush(); os.fsync(transcript.fileno())
            tasks = [asyncio.create_task(read_output()), asyncio.create_task(read_error())]
            payload = {"type": "user", "session_id": state["session_id"], "message": {"role": "user", "content": content}}
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await process.stdin.drain(); process.stdin.close()
            waiting = asyncio.gather(*tasks, process.wait())
            stopped = asyncio.create_task(engine.abort_event.wait())
            failed = asyncio.create_task(host.failed.wait())
            tasks.extend([stopped, failed])
            done, _ = await asyncio.wait({waiting, stopped, failed}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                waiting.cancel(); await asyncio.gather(waiting, return_exceptions=True)
                raise AbortRequestedError("External execution was stopped by its owner")
            if failed in done:
                waiting.cancel(); await asyncio.gather(waiting, return_exceptions=True)
                raise ExternalExecutionError(host.failure, "External execution stopped: " + host.failure + "; preserved operations were not replayed")
            await waiting
            if host.failure:
                raise ExternalExecutionError(host.failure, "External execution stopped: " + host.failure + "; preserved operations were not replayed")
            if process.returncode or not initialized or not final or final.get("subtype") != "success" or final.get("is_error") is not False or final.get("terminal_reason") != "completed" or not isinstance(final.get("result"), str):
                raise ExternalExecutionError("external_execution_error", "External program did not return a successful owned completion receipt; inspect its saved transcript")
            state.update(phase="idle", has_session=True, result=final["result"])
            await host.checkpoint()
            return response(final["result"], host.usage), [], False
    except (asyncio.CancelledError, AbortRequestedError, PersistenceError, ExternalExecutionError):
        raise
    except Exception as error:
        raise ExternalExecutionError("external_execution_error", "External execution failed before a valid completion receipt; inspect its saved transcript") from error
    finally:
        if process is not None and process.returncode is None:
            process.kill(); await process.wait()  # Only the child created above, never an unrelated CLI.
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        config_path.unlink(missing_ok=True)
        if state["phase"] == "running":
            state["phase"] = "interrupted"
            if not engine.persist_session_state():
                raise PersistenceError("External cancellation/failure could not be checkpointed; inspect saved execution before retrying")
