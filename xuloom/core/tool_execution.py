"""The engine's single authorized tool-execution path, shared by streaming and batch calls."""
import asyncio
import copy
import hashlib
import json
import os
import re
from pydantic import ValidationError
from xuloom.context.tool_results import build_tool_result, serialize_tool_result
from xuloom.core.storage import artifact_directory
from xuloom.core.run_result import PersistenceError
from xuloom.protocols.dsml import ToolProtocolError


def tool_identity(engine, tool_name, tool_inputs):
    turn_id = next((m.get("_uuid", "") for m in reversed(engine.messages)
        if m.get("role") == "user" and m.get("_origin", "human") == "human"
        and not m.get("_is_meta") and not m.get("_is_tool_result")), "")
    digest = hashlib.sha256(json.dumps([tool_name, tool_inputs], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return turn_id, digest


def restore_tool_activity(engine, metadata):
    activity = metadata.get("tool_activity", {})
    engine.in_progress_tools = copy.deepcopy(activity.get("in_progress", {}))
    for row in engine.in_progress_tools.values():
        if row.get("phase") == "executing":
            row["recovery_uncertain"] = True
    engine.recent_tool_activity = copy.deepcopy(activity.get("recent", []))
    results = {b.get("tool_use_id") for m in engine.messages for b in m.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_result"}
    from xuloom.core.messages import create_tool_result_message, create_user_message
    linked = set()
    for message in list(engine.messages):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        recovered = []
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("id") in results:
                continue
            tool_id = block.get("id")
            linked.add(tool_id)
            row = next((row for row in engine.recent_tool_activity if row.get("tool_use_id") == tool_id and row.get("tool_result")), None)
            if row:
                recovered.append(copy.deepcopy(row["tool_result"]))
            elif tool_id in engine.in_progress_tools:
                recovered.append({"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                    "content": "Interrupted during execution. Effects are uncertain; this operation was not replayed. Inspect existing state before requesting approval for a new attempt."})
        if recovered:
            position = engine.messages.index(message)
            engine.messages.insert(position + 1, create_tool_result_message(recovered, source_assistant_uuid=message.get("_uuid")))
            results.update(r["tool_use_id"] for r in recovered)
    unlinked = [row for row in engine.recent_tool_activity if row.get("tool_result") and row.get("tool_use_id") not in linked | results]
    uncertain = list(engine.in_progress_tools.values())
    if unlinked or uncertain:
        engine.messages.append(create_user_message("Recovery checkpoint: completed results are saved; uncertain operations were NOT replayed. Inspect current state before any new mutation.\n" +
            json.dumps({"saved_results": unlinked, "uncertain_operations": uncertain}, ensure_ascii=False), is_meta=True, origin="tool_recovery"))


async def execute_tool(self, tool_block, sys_print_callback=print, *, turn: int = 0, event_callback=None, result_callback=None):
    from xuloom.core.session_lock import SessionBusyError, SessionConflictError
    try:
        self.require_writer()
    except (SessionBusyError, SessionConflictError) as error:
        return {"type": "tool_result", "tool_use_id": self._tb_get(tool_block, "id", ""), "content": str(error), "is_error": True}
    tool_name = self._tb_get(tool_block, "name", "")
    tool_id = self._tb_get(tool_block, "id", "")
    tool_inputs = copy.deepcopy(self._tb_get(tool_block, "input", {}) or {})
    input_error = bool(self._tb_get(tool_block, "_argument_error", ""))
    handler_invoked = False
    raw_result = None
    native_content = None
    native_errors = []
    status = "error"
    user_turn_id, digest = tool_identity(self, tool_name, tool_inputs)
    for row in self.recent_tool_activity:
        if user_turn_id and row.get("tool_use_id") == tool_id and row.get("user_turn_id") == user_turn_id and row.get("tool_result"):
            if row.get("input_digest") == digest:
                return copy.deepcopy(row["tool_result"])
            return {"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                "content": "Tool call ID already has a durable result for different arguments; it was not executed again."}
    uncertain_operations = [row for row in self.in_progress_tools.values() if row.get("recovery_uncertain")]
    if any(row.get("tool_use_id") == tool_id for row in uncertain_operations):
        return {"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
            "content": "This interrupted tool call has uncertain effects and cannot be automatically replayed. Inspect existing state and request approval for a new attempt."}
    spill_recorder = self._build_spill_recorder(tool_name=tool_name, tool_use_id=tool_id)

    sys_print_callback(f"  [dim cyan]↳ calling {tool_name}[/dim cyan]")
    self._record_tool_start(tool_name, tool_id, tool_inputs)
    before_tool_decision = self.hook_manager.evaluate_before_tool_hooks({
        "tool_name": tool_name,
        "tool_use_id": tool_id,
        "tool_input": tool_inputs,
        "mode": self.get_mode(),
        "agent_id": self.agent_id,
        "agent_role": self.agent_role,
    })
    before_tool_prefix = ""
    if before_tool_decision.outputs:
        before_tool_prefix = "\n".join(
            f"[Hook:BeforeToolUse #{index}] {content}"
            for index, content in enumerate(before_tool_decision.outputs, start=1)
        )
        before_tool_prefix += "\n\n"
    tool_inputs = copy.deepcopy(before_tool_decision.updated_input or tool_inputs)

    if before_tool_decision.history:
        await self._record_loop_transition(
            "before_tool_hook",
            turn=turn,
            event_callback=event_callback,
            tool_name=tool_name,
            tool_use_id=tool_id,
            behavior=before_tool_decision.behavior,
            hook_count=len(before_tool_decision.history),
            input_changed=any(item.get("input_changed") for item in before_tool_decision.history),
            reason=before_tool_decision.reason,
        )

    if self._tb_get(tool_block, "_argument_error", ""):
        res_text = serialize_tool_result(build_tool_result(
            ok=False, content=(self._tb_get(tool_block, "_argument_error", "") +
                "\nThis call was not executed. Correct the arguments using the tool schema and resubmit only this call; do not repeat completed operations."),
            metadata={"tool_name": tool_name, "executed": False, "code": "invalid_json_arguments"}, is_error=True))
    elif tool_name not in self.available_tools:
        input_error = True
        res_text = serialize_tool_result(build_tool_result(ok=False,
            content=f"Tool '{tool_name}' is unavailable. This call was not executed. Use a currently declared tool or discover it with search_tool.",
            metadata={"code": "unavailable_tool", "executed": False}, is_error=True))
    elif before_tool_decision.behavior == "reject":
        res_text = serialize_tool_result(
            build_tool_result(
                ok=False,
                content=before_tool_decision.reason or f"Tool '{tool_name}' rejected by BeforeToolUse hook.",
                metadata={
                    "tool_name": tool_name,
                    "tool_use_id": tool_id,
                    "hook_behavior": before_tool_decision.behavior,
                    "hook_count": len(before_tool_decision.history or []),
                    "hook_updated_input": tool_inputs,
                },
                is_error=True,
            ),
            artifact_dir=str(artifact_directory(self.session_cwd) / self.session_id),
            spill_recorder=spill_recorder,
        )
    else:
        effective_tools = {}
        try:
            effective_tools = self._get_effective_tools()
            if tool_name not in effective_tools:
                raise KeyError(f"Tool '{tool_name}' is not available in current mode.")
            handler = effective_tools[tool_name]
            confinement = self.execution_sandbox
            confinement.check_tool(tool_name)
            normalized_input = tool_inputs
            if hasattr(handler, "input_schema"):
                validated_input = handler.input_schema(**tool_inputs)
                normalized_input = validated_input.model_dump()
                tool_inputs = validated_input.model_dump(exclude_unset=True)
            prepare_input = getattr(handler, "prepare_input", None)
            if callable(prepare_input):
                # Permissions inspect exactly the paths that will execute.
                normalized_input = prepare_input(normalized_input)
                tool_inputs = prepare_input(tool_inputs)
            validate_input = getattr(handler, "validate_input", None)
            validation_error = validate_input(**normalized_input) if callable(validate_input) else None
            if isinstance(validation_error, str) and validation_error.strip():
                raise ToolProtocolError(validation_error)
            permission = await self.permission_manager.authorize(
                handler,
                tool_inputs,
                context={
                    "turn": turn,
                    "tool_use_id": tool_id,
                    "agent_id": self.agent_id,
                    "agent_role": self.agent_role,
                    "recovery_uncertain": bool(uncertain_operations),
                },
            )
            if permission.behavior != "allow":
                await self._record_loop_transition(
                    "permission_denied",
                    turn=turn,
                    event_callback=event_callback,
                    tool_name=tool_name,
                    tool_use_id=tool_id,
                    source=permission.source,
                    reason=permission.reason,
                    denial_count=(permission.metadata or {}).get("denial_count_after"),
                )
                res_text = serialize_tool_result(
                    build_tool_result(
                        ok=False,
                        content=(
                            permission.reason
                            or f"Permission denied for tool '{tool_name}'."
                        ),
                        metadata={
                            "tool_name": tool_name,
                            "tool_use_id": tool_id,
                            "permission_behavior": permission.behavior,
                            "permission_source": permission.source,
                            **dict(permission.metadata or {}),
                        },
                        is_error=True,
                    ),
                    artifact_dir=str(artifact_directory(self.session_cwd) / self.session_id),
                    spill_recorder=spill_recorder,
                )
            else:
                self.in_progress_tools[tool_id].update(user_turn_id=user_turn_id, input_digest=digest, phase="executing")
                if not self.persist_session_state():
                    raise PersistenceError("Could not persist execution intent; tool was not executed. " + self.session_manager.last_save_error)
                if not self.permission_manager._is_read_only(handler, tool_inputs):
                    for row in uncertain_operations:
                        if row.get("tool_use_id") != tool_id:
                            self.in_progress_tools.pop(row.get("tool_use_id"), None)
                handler_invoked = True
                from xuloom.security.execution import FILE_TOOLS
                if confinement.enabled and tool_name in FILE_TOOLS:
                    raw_result = await confinement.tool(tool_name, tool_inputs, self.tool_context)
                else:
                    raw_result = await handler(**tool_inputs)
                from xuloom.core.model_inputs import filter_tool_media
                raw_result = filter_tool_media(raw_result, self._input_contract(self.tool_context.get("request_model", self.primary_model)))
                from xuloom.context.tool_results import native_tool_content
                native_content = native_tool_content(raw_result, errors=native_errors,
                    artifact_dir=str(artifact_directory(self.session_cwd) / self.session_id), spill_recorder=spill_recorder)
                res_text = serialize_tool_result(
                    "\n".join(part["text"] for part in native_content if part.get("type") == "text") + "\n[Media blocks retained in tool receipt.]" if native_content is not None else raw_result,
                    spill_threshold_chars=tool_inputs.get("max_chars", 100000) if tool_name == "skill_tool" else 5000,
                    artifact_dir=str(artifact_directory(self.session_cwd) / self.session_id),
                    spill_recorder=spill_recorder,
                )
                if isinstance(raw_result, dict) and {"ok", "is_error"}.issubset(raw_result):
                    failed = not raw_result["ok"] or raw_result["is_error"] or res_text.startswith("Status: error")
                else:
                    # Legacy string tools use a leading error marker; ordinary result text is not a status protocol.
                    failed = str(res_text).lstrip().startswith(("Permission denied", "Error:", "Error reading file:",
                        "Sub-agent spawn denied:", "Status: error", "Tool Exception Caught"))
                    if isinstance(raw_result, list):
                        failed = any(isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").startswith("Error:") for block in raw_result)
                if tool_name == "command_tool" and isinstance(raw_result, str):
                    command_receipt = json.loads(raw_result)
                    failed = isinstance(command_receipt, dict) and command_receipt.get("status") in {
                        "failed", "cancelled", "interrupted", "timed_out", "output_limit", "storage_error", "output_incomplete"}
                status = "error" if failed or native_errors else "ok"
        except asyncio.CancelledError:
            if handler_invoked:
                self.in_progress_tools[tool_id]["recovery_uncertain"] = True
            else:
                self.in_progress_tools.pop(tool_id, None)
            self._emit_tool_activity("tool_result", tool_name, tool_id, result="操作已取消。", is_error=True, status="cancelled")
            raise
        except (ValidationError, ToolProtocolError) as e:
            detail = json.dumps(e.errors(include_input=False, include_url=False, include_context=False), ensure_ascii=False, default=str) if isinstance(e, ValidationError) else str(e)
            if handler_invoked:
                res_text = "Tool Exception Caught: " + detail + "\nExecution may have begun. Inspect the existing operation and its durable result before retrying; do not blindly repeat it."
            else:
                input_error = True
                errors = e.errors(include_input=False, include_url=False, include_context=False) if isinstance(e, ValidationError) else []
                hint_builder = getattr(effective_tools.get(tool_name), "input_error_hint", None)
                hint = str(hint_builder() or "").strip() if callable(hint_builder) else ""
                res_text = serialize_tool_result(build_tool_result(ok=False,
                    content="Invalid tool input: " + detail + "\nCorrect the parameters using the tool schema. This call was not executed; do not repeat completed operations." + ("\n" + hint if hint else ""),
                    metadata={"code": "invalid_tool_input", "tool_name": tool_name, "executed": False,
                        **({"validation_errors": errors} if errors else {})}, is_error=True))
        except PersistenceError:
            raise
        except Exception as e:
            res_text = serialize_tool_result(build_tool_result(ok=False,
                content=f"Tool Exception Caught: {str(e)}" + (
                    "\nExecution may have begun. Inspect existing state and saved results before retrying; do not blindly repeat the operation."
                    if handler_invoked else "\nThe handler was not invoked."),
                metadata={"code": "tool_execution_error", "handler_invoked": handler_invoked,
                    "effects": "uncertain" if handler_invoked else "none"}, is_error=True))

    if before_tool_prefix:
        res_text = before_tool_prefix + str(res_text)
        if native_content is not None:
            native_content.insert(0, {"type": "text", "text": before_tool_prefix})

    receipt = {"type": "tool_result", "tool_use_id": tool_id, "content": native_content if native_content is not None else res_text,
               "is_error": status == "error", **({"_input_error": True} if input_error else {})}
    from xuloom.context.tool_media import archive_images
    media_owner = (self.tool_context.get("delegation") or {}).get("owner_session_id") or self.session_id
    if not isinstance(media_owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_owner):
        media_owner = self.session_id
    media = archive_images(native_content, artifact_directory(self.session_cwd) / media_owner,
        name=os.path.basename(str(tool_inputs.get("file_path") or tool_inputs.get("path") or ""))) if native_content is not None else []
    self.in_progress_tools.pop(tool_id, None)
    self.recent_tool_activity.insert(0, {"tool_name": tool_name, "tool_use_id": tool_id, "status": status,
        "result_preview": str(res_text)[:280], "tool_result": receipt, "user_turn_id": user_turn_id, "input_digest": digest, "media": media})
    # Retain every receipt in the active turn, including batches larger than the UI preview limit.
    current = [row for row in self.recent_tool_activity if row.get("user_turn_id") == user_turn_id]
    older = [row for row in self.recent_tool_activity if row.get("user_turn_id") != user_turn_id]
    self.recent_tool_activity = current + older[:12]
    from xuloom.features.session_summary import record_receipt
    record_receipt(self.artifact_tracker.references,
        turn_id=(self.tool_context.get("delegation") or {}).get("owner_turn_id") or user_turn_id,
        agent_id=self.agent_id, tool_name=tool_name, tool_use_id=tool_id,
        result=raw_result, status=status)
    self.session_memory_manager.record_tool_call()
    self._sync_message_context()
    if not self.persist_session_state():
        # Keep the pre-execution checkpoint authoritative. Recovery must not assume absence of effects.
        self.recent_tool_activity = [row for row in self.recent_tool_activity if row.get("tool_use_id") != tool_id]
        self.in_progress_tools[tool_id] = {"tool_name": tool_name, "tool_use_id": tool_id,
            "status": "running", "phase": "executing", "recovery_uncertain": True, "user_turn_id": user_turn_id, "input_digest": digest}
        raise PersistenceError("Tool may have executed but its result could not be persisted; do not replay it automatically. " + self.session_manager.last_save_error)
    self.attachment_collector.track_tool_use(tool_name)
    self._emit_tool_activity("tool_result", tool_name, tool_id, result=str(res_text)[:16000], is_error=receipt["is_error"], status=status, media=media)
    sys_print_callback(f"    [dim]{tool_name} -> {status}[/dim]")
    if callable(result_callback) and not receipt["is_error"]:
        result_callback(copy.deepcopy(raw_result))
    return receipt


async def execute_tool_batches(self, tool_calls, sys_print_callback=print, *, turn: int = 0, event_callback=None):
    results = []
    batches = self._partition_tool_batches(tool_calls)
    semaphore = asyncio.Semaphore(self.MAX_TOOL_CONCURRENCY)

    for batch in batches:
        blocks = batch["blocks"]
        if batch["is_concurrency_safe"]:
            sys_print_callback(
                f"[bold dim yellow]❯ Executing {len(blocks)} concurrency-safe tool call(s) in parallel "
                f"(max concurrency={self.MAX_TOOL_CONCURRENCY})...[/bold dim yellow]"
            )
            sibling_cancel = asyncio.Event()

            async def _run_with_sibling_cancel(tc):
                async with semaphore:
                    if sibling_cancel.is_set():
                        tool_id = self._tb_get(tc, "id", "")
                        tool_name = self._tb_get(tc, "name", "")
                        return {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": f"Cancelled: sibling tool in batch failed ({tool_name}).",
                            "is_error": True,
                        }
                    res = await self._execute_tool_wrapper(
                        tc, sys_print_callback, turn=turn, event_callback=event_callback,
                    )
                    if res.get("is_error") and self._tb_get(tc, "name", "") in self.SIBLING_CANCEL_TOOLS:
                        sibling_cancel.set()
                    return res

            batch_results = await asyncio.gather(
                *(_run_with_sibling_cancel(tc) for tc in blocks)
            )
            results.extend(batch_results)
        else:
            sys_print_callback(
                f"[bold dim yellow]❯ Executing {len(blocks)} mutating/high-risk tool call(s) serially...[/bold dim yellow]"
            )
            for tool_block in blocks:
                results.append(
                    await self._execute_tool_wrapper(
                        tool_block,
                        sys_print_callback,
                        turn=turn,
                        event_callback=event_callback,
                    )
                )

    return results
