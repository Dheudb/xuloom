"""Optional installed Claude Code as a sampler, using the existing owned runtime."""
import asyncio
import json
import os
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import atomic_text
from .host import LocalHost, LIMIT
from .program import environment
from .runtime import prepare, cleanup, response, ExternalExecutionError


async def sample(engine, *, system_prompt, api_messages, turn_count, model_name, max_tokens,
                 event_callback=None, **_):
    from xuloom.core.engine import AbortRequestedError
    prepared = await prepare(engine, system_prompt=system_prompt, api_messages=api_messages, model_name=model_name)
    if prepared.get("cached") is not None:
        return prepared["cached"], [], False
    state, home, content = prepared["state"], prepared["home"], prepared["content"]
    binding = engine.tool_context["external_backend"]
    config_path = home / "connection.json"
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
                HTTP_PROXY=host.url, HTTPS_PROXY=host.url, NO_PROXY="127.0.0.1,localhost,::1",
                ENABLE_TOOL_SEARCH="false")  # Native discovery controls model visibility.
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
        await cleanup(engine, state, process, tasks, [config_path])
