"""Installed Pi RPC sampler; owned MCP, checkpoints and budgets remain native."""
import asyncio
import json
import os
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import atomic_text, IOPath, logical_path
from .host import LocalHost, LIMIT
from .program import environment, _hash
from .runtime import ExternalExecutionError, prepare, cleanup, response
from .pi_history import current_messages, verify_file, verify_receipts


async def sample(engine, *, system_prompt, api_messages, turn_count, model_name, max_tokens,
                 event_callback=None, **_):
    from xuloom.core.engine import AbortRequestedError
    prepared = await prepare(engine, system_prompt=system_prompt, api_messages=api_messages, model_name=model_name)
    if prepared.get("cached") is not None: return prepared["cached"], [], False
    state, home, content = prepared["state"], prepared["home"], prepared["content"]
    binding = engine.tool_context["external_backend"]
    models_path, mcp_path = home / "config/models.json", home / "config/mcp.json"
    process, tasks, pending, settled = None, [], {}, asyncio.Queue()
    def owned_history(value):
        if not isinstance(value, str) or not value:
            raise ExternalExecutionError("external_session_missing", "The external session history reference is missing")
        path = logical_path(IOPath(value).resolve())
        if not path.is_relative_to(logical_path(IOPath(home / "sessions").resolve())):
            raise ExternalExecutionError("external_history_changed", "External history points outside its owned session directory; it was not read")
        return path
    try:
        async with LocalHost(engine, state, event_callback=event_callback, turn=turn_count) as host:
            host.maximum = max_tokens
            await host.checkpoint()
            inputs = engine._input_contract(model_name)
            # Unknown aliases retain native passthrough semantics. Pi must not silently
            # discard an image before the selected provider can accept or reject it.
            wire_inputs = ["text", "image"] if inputs is None or "image" in inputs else ["text"]
            profile = engine.reasoning_profile
            thinking = engine.reasoning_effort or (profile or {}).get("effort", "default")
            if profile and profile["thinking"] == "disabled": thinking = "off"
            elif thinking == "default": thinking = "medium" if profile and profile["thinking"] == "enabled" else "off"
            if thinking == "none": thinking = "off"
            thinking = thinking or "off"
            base_url = host.url + ("/v1" if engine.model_provider == "openai" else "")
            api = "openai-completions" if engine.model_provider == "openai" else "anthropic-messages"
            atomic_text(models_path, json.dumps({"providers": {"xuloom": {"baseUrl": base_url, "api": api, "apiKey": host.token,
                "models": [{"id": model_name, "name": model_name, "reasoning": thinking != "off", "input": wire_inputs,
                    "contextWindow": int(os.environ.get("XULOOM_CONTEXT_WINDOW", "0")) or 200000,
                    "maxTokens": max_tokens, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
            atomic_text(mcp_path, json.dumps({"autoEnableCodemode": False, "mcpServers": {"xuloom": {"url": host.url + "/mcp",
                "headers": {"Authorization": "Bearer " + host.token}, "exposure": "direct"}}}))
            if state["has_session"]:
                saved_file = owned_history(state.get("session_file"))
                if not IOPath(saved_file).is_file():
                    raise ExternalExecutionError("external_session_missing", "The external session checkpoint is missing; a blank conversation was not created")
                if state.get("history_sha256") and _hash(IOPath(saved_file)) != state["history_sha256"]:
                    raise ExternalExecutionError("external_history_changed", "External history changed after its confirmed checkpoint; the task was not replayed")
            env = environment(home)
            env.update(HTTP_PROXY=host.url, HTTPS_PROXY=host.url, NO_PROXY="127.0.0.1,localhost,::1")
            # Resume the verified file explicitly. Pi's project-ID lookup can miss
            # the same Windows directory when a new CLI uses its long-path form.
            session_args = ["--session", str(saved_file)] if state["has_session"] else ["--session-id", state["session_id"]]
            args = [binding["runtime"], binding["program"], "--mode", "rpc", "--provider", "xuloom", "--model", model_name,
                *session_args, "--session-dir", str(home / "sessions"), "--no-extensions", "-e", "builtin:mcp",
                "--no-builtin-tools", "--tools", "mcp__xuloom__*", "--no-approve", "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-themes",
                "--offline", "--system-prompt", str(home / "instructions.txt")]
            args.extend(["--thinking", thinking])
            process = await asyncio.create_subprocess_exec(*args, cwd=engine.session_cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LIMIT,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            async def read_output():
                try:
                    with (home / (uuid.uuid4().hex + ".jsonl")).open("xb") as transcript:
                        while line := await process.stdout.readline():
                            transcript.write(line)
                            row = json.loads(line)
                            if not isinstance(row, dict): raise ExternalExecutionError("protocol_error", "Invalid external RPC record")
                            if row.get("type") == "response":
                                call = pending.get(row.get("id"))
                                if call is not None and not call.done(): call.set_result(row)
                            if row.get("type") == "agent_settled": await settled.put(row)
                            if row.get("type") == "message_update":
                                delta = row.get("assistantMessageEvent", {})
                                if delta.get("type") in {"text_delta", "thinking_delta"}:
                                    await engine._emit_stream_event(event_callback, {"type": delta["type"], "turn": turn_count, "text": delta.get("delta", "")})
                        transcript.flush(); os.fsync(transcript.fileno())
                finally:
                    for future in pending.values():
                        if not future.done(): future.set_exception(ExternalExecutionError("external_process_exited", "External RPC ended before its command receipt"))
            async def read_error():
                with (home / (uuid.uuid4().hex + ".stderr")).open("xb") as transcript:
                    while chunk := await process.stderr.read(65536): transcript.write(chunk)
                    transcript.flush(); os.fsync(transcript.fileno())
            reader = asyncio.create_task(read_output())
            error_reader = asyncio.create_task(read_error())
            tasks.extend([reader, error_reader])
            async def rpc(kind, **values):
                identity = uuid.uuid4().hex; future = asyncio.get_running_loop().create_future(); pending[identity] = future
                try:
                    process.stdin.write((json.dumps({"id": identity, "type": kind, **values}, ensure_ascii=False) + "\n").encode())
                    await process.stdin.drain()
                    answer = await asyncio.wait_for(future, 30)
                    if answer.get("command") != kind or answer.get("success") is not True:
                        raise ExternalExecutionError("external_rpc_error", "External command did not return a successful matching receipt: " + kind)
                    return answer.get("data")
                finally: pending.pop(identity, None)
            def validate(snapshot):
                if not isinstance(snapshot, dict) or snapshot.get("sessionId") != state["session_id"] or snapshot.get("model", {}).get("id") != model_name or snapshot.get("model", {}).get("baseUrl") != base_url:
                    raise ExternalExecutionError("protocol_error", "External RPC changed its frozen session/model")
                if snapshot.get("isStreaming") or snapshot.get("isCompacting") or snapshot.get("pendingMessageCount") != 0:
                    raise ExternalExecutionError("protocol_error", "External session is not idle")
                return str(owned_history(snapshot.get("sessionFile")))
            initial = await rpc("get_state"); session_file = validate(initial)
            if state["has_session"] and (saved_file != owned_history(session_file) or initial.get("messageCount", 0) != state.get("message_count", 0)):
                raise ExternalExecutionError("external_session_missing", "External history was replaced or lost; the task was not replayed")
            base_cursor = state.get("entry_cursor")
            cursor_args = {"since": base_cursor} if base_cursor else {}
            initial_export = await rpc("get_entries", **cursor_args)
            initial_leaf = initial_export["leafId"]
            if not settled.empty(): raise ExternalExecutionError("protocol_error", "External run settled without an owned prompt")
            texts, images = [], []
            for block in content:
                if block.get("type") == "text": texts.append(block["text"])
                elif block.get("type") == "image" and block.get("source", {}).get("type") == "base64":
                    source = block["source"]; images.append({"type": "image", "data": source["data"], "mimeType": source["media_type"]})
                else: raise ExternalExecutionError("unsupported_input", "This external RPC does not support the supplied input block")
            answer = await rpc("prompt", message="\n\n".join(texts), **({"images": images} if images else {}))
            if not isinstance(answer, dict) or answer.get("disposition") != "started":
                raise ExternalExecutionError("protocol_error", "External task did not start in its owned idle session")
            waiting = asyncio.create_task(settled.get()); stopped = asyncio.create_task(engine.abort_event.wait()); failed = asyncio.create_task(host.failed.wait())
            tasks.extend([waiting, stopped, failed])

            async def checkpoint_history(interrupted=False):
                after = await rpc("get_state"); session_file = validate(after)
                exported = await rpc("get_entries", **cursor_args)
                messages = current_messages(exported, initial_leaf, base_cursor)
                if not any(message.get("role") == "user" for message in messages):
                    raise ExternalExecutionError("external_outcome_unknown", "External history did not retain its owned prompt")
                verify_receipts(host, messages)
                if host.failure: raise ExternalExecutionError(host.failure, "External execution has an unresolved provider or tool failure")
                last = messages[-1]
                if not interrupted and (last.get("role") != "assistant" or last.get("stopReason") != "stop"):
                    raise ExternalExecutionError("external_execution_error", "External agent settled without a successful final assistant message")
                text = "\n".join(block["text"] for block in last.get("content", []) if block.get("type") == "text") if not interrupted else ""
                # The stopped RPC process can no longer append after this fingerprint.
                if process.returncode is None: process.kill()
                await process.wait()
                await asyncio.wait_for(asyncio.gather(reader, error_reader), 5)
                proof = verify_file(session_file, state, exported)
                state.update(**proof, phase="idle", has_session=True, session_file=session_file, message_count=after["messageCount"])
                if interrupted:
                    state["stop_checkpoint"] = {"confirmed": True, "reason": "owner_cancelled", "tool_ids": sorted(host.receipts)}
                else: state["result"] = text
                try:
                    await host.checkpoint()
                except BaseException:
                    state["phase"] = "interrupted"
                    state.pop("stop_checkpoint", None)
                    raise
                return text

            async def confirm_stop():
                if state["phase"] != "running": return
                host.stopping = True
                try:
                    cleared = await rpc("clear_queue")
                    if not isinstance(cleared, dict) or cleared.get("steering") != [] or cleared.get("followUp") != []:
                        raise ExternalExecutionError("external_outcome_unknown", "External queued messages were not owned by this run")
                    await rpc("abort")
                    await asyncio.shield(waiting)
                    # Settle outstanding provider reservations and drain the native tool gate.
                    active = list(host.tasks)
                    for task in active: task.cancel()
                    await asyncio.gather(*active, return_exceptions=True)
                    async with host.execution_lock:
                        await checkpoint_history(interrupted=True)
                except BaseException as error:
                    state["stop_error"] = getattr(error, "reason", "stop_checkpoint_unconfirmed")
                    state["phase"] = "interrupted"
                    state.pop("stop_checkpoint", None)
                    raise

            host.on_interrupt = confirm_stop
            done, _ = await asyncio.wait({waiting, reader, stopped, failed}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                await asyncio.wait_for(confirm_stop(), 10)
                raise AbortRequestedError("External execution was stopped by its owner")
            if failed in done: raise ExternalExecutionError(host.failure, "External execution stopped: " + host.failure)
            if waiting not in done:
                await reader
                raise ExternalExecutionError("external_process_exited", "External RPC exited before final idle")
            final = waiting.result()
            if final.get("aborted") is not False:
                raise ExternalExecutionError("external_execution_error", "External agent settled without a successful final assistant message")
            text = await checkpoint_history()
            return response(text, host.usage), [], False
    except (asyncio.CancelledError, AbortRequestedError, PersistenceError, ExternalExecutionError):
        raise
    except Exception as error:
        raise ExternalExecutionError("external_execution_error", "External RPC did not return a valid owned result; inspect its saved transcript") from error
    finally:
        await cleanup(engine, state, process, tasks, [models_path, mcp_path])
