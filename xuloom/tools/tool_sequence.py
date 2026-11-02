"""Receipt-guarded, literal actions over the existing authorized executor.

Inspired by HarnessMM's native bank boundary; no selector, provider, or second
execution journal. Unknown observations return control to the calling model.
"""
import asyncio
import copy
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from xuloom.context.tool_results import build_tool_result
from xuloom.protocols.dsml import ToolProtocolError
from xuloom.tools.base import BaseAgenticTool


class ReceiptGuard(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    step: str = Field(description="ID of an earlier step whose real result is checked")
    path: list[str] = Field(min_length=1, max_length=12, description="Object keys in the complete JSON tool result; no expressions")
    equals: JsonValue


class LiteralStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    tool: str = Field(min_length=1, max_length=160)
    arguments: dict[str, JsonValue]
    when: list[ReceiptGuard] = Field(default_factory=list, description="All guards must match; missing/false evidence defers remaining steps to the model")


class ToolSequenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    steps: list[LiteralStep] = Field(min_length=1, max_length=128)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def observation(value):
    # Never infer control state from prose, truncated output, or media blocks.
    if isinstance(value, str):
        try:
            def unique(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("Ambiguous receipt")
                    result[key] = item
                return result
            value = json.loads(value, object_pairs_hook=unique)
        except ValueError:
            return None
    if isinstance(value, dict) and isinstance(value.get("metadata"), dict) and value["metadata"].get("truncated") is True:
        return None
    return value if isinstance(value, dict) else None


def guard_matches(guard, results):
    value = observation(results.get(guard.step))
    if guard.path[:2] == ["output", "text"] and isinstance(value, dict):
        output = value.get("output")
        if not isinstance(output, dict) or output.get("eof") is not True or output.get("has_more") is not False:
            return False
    for key in guard.path:
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    try:
        return digest(value) == digest(guard.equals)
    except (ValueError, TypeError):
        return False


def scope(engine):
    sandbox = engine.sandbox_manager.active_sandbox or {}
    return digest([engine.session_id, engine.agent_id, engine.session_cwd, engine.model_provider,
                   engine.primary_model, engine.api_base_url, engine._api_key, engine.get_mode(),
                   engine.execution_sandbox.mode, sandbox.get("path"),
                   engine.permission_manager.approval_policy.revision,
                   [m.get("_uuid") for m in engine.messages if m.get("role") == "user"]])


def tool_contract(engine, name):
    handler = engine._get_effective_tools().get(name)
    return digest([id(handler), handler.get_tool_definition()]) if handler is not None else None


def sequence_current(engine, expected, name=None, contract=None):
    mailbox = engine.tool_context.get("delegated_mailbox")
    return not engine.abort_event.is_set() and scope(engine) == expected and not (callable(mailbox) and mailbox()) and (name is None or tool_contract(engine, name) == contract)


async def execute_sequence(engine, identity, steps, *, turn, event_callback, sys_print_callback):
    plan = ToolSequenceInput(steps=steps)
    frozen = scope(engine)
    names = set()
    contracts = {}
    # Admit the entire fixed plan before the first side effect. The executor
    # checks current visibility, schema, hooks, permissions and confinement again.
    for step in plan.steps:
        if step.id in names or step.tool == ToolSequenceTool.name:
            raise ToolProtocolError("Sequence IDs must be unique; recursive sequences are unsupported")
        if any(guard.step not in names or any(not key for key in guard.path) for guard in step.when):
            raise ToolProtocolError("Guards must reference earlier steps using nonempty object keys")
        handler = engine._get_effective_tools().get(step.tool)
        if handler is None:
            raise ToolProtocolError("A sequence requires currently available tools; discover tools before submitting it")
        engine.execution_sandbox.check_tool(step.tool)
        normalized = step.arguments
        if getattr(handler, "input_schema", None) is not None:
            if set(step.arguments) - set(handler.input_schema.model_fields):
                raise ToolProtocolError("Sequence arguments contain unknown tool fields")
            normalized = handler.input_schema.model_validate(step.arguments, strict=True).model_dump()
        validator = getattr(handler, "validate_input", None)
        error = validator(**normalized) if callable(validator) else None
        if isinstance(error, str) and error:
            raise ToolProtocolError("Sequence contains invalid tool input; inspect its tool schema")
        digest(step.arguments)
        contracts[step.tool] = tool_contract(engine, step.tool)
        names.add(step.id)
    results, receipts, reason = {}, [], "completed"
    for step in plan.steps:
        if engine.goal:
            from xuloom.features.goals import pending_control
            await pending_control(engine)
        if engine.abort_event.is_set():
            raise asyncio.CancelledError()
        if not sequence_current(engine, frozen):
            reason = "new_input_or_scope_changed"
            break
        if tool_contract(engine, step.tool) != contracts[step.tool]:
            reason = "tool_contract_changed"
            break
        if not all(guard_matches(guard, results) for guard in step.when):
            reason = "guard_not_proven"
            break
        captured = []
        call_id = "sequence-" + digest([identity, step.id])[:40]
        receipt = await engine._execute_tool_wrapper({"id": call_id, "name": step.tool,
            "input": copy.deepcopy(step.arguments), "_literal_input": True, "_literal_scope": frozen,
            "_literal_contract": contracts[step.tool]},
            sys_print_callback, turn=turn, event_callback=event_callback,
            result_callback=lambda result: captured.append(result))
        receipts.append({"step": step.id, "tool": step.tool, "receipt": receipt})
        await engine._record_loop_transition("tool_sequence_step", turn=turn, event_callback=event_callback,
            sequence_id=identity, step_id=step.id, tool_name=step.tool,
            action_digest=digest([step.tool, step.arguments]), receipt_digest=digest(receipt),
            call_status="error" if receipt.get("is_error") else "returned")
        if receipt.get("is_error"):
            reason = "tool_failed"
            break
        if not captured:
            # An existing receipt may have been reused by the execution journal.
            # Its display text is not fresh structured evidence for another action.
            reason = "fresh_observation_unavailable"
            break
        results[step.id] = captured[0]
        if step.tool == "command_tool":
            command = observation(captured[0]) or {}
            if command.get("status") != "completed" or type(command.get("exit_code")) is not int or command["exit_code"] != 0:
                reason = "command_not_successful"
                break
    pending = [step.id for step in plan.steps[len(receipts):]]
    data = {"status": "completed" if reason == "completed" else "deferred", "reason": reason,
            "steps": receipts, "pending": pending,
            "message": "Execution of the declared steps ended; domain acceptance is unchanged." if not pending and reason == "completed" else
                       "Return control to the model. Preserve completed actions; inspect the evidence before continuing."}
    return build_tool_result(ok=reason != "tool_failed", is_error=reason == "tool_failed",
        content=json.dumps(data, ensure_ascii=False),
        metadata={"sequence_id": identity, "planned_steps": len(plan.steps), "attempted_steps": len(receipts),
                  "successful_tool_calls": sum(not item["receipt"].get("is_error") for item in receipts),
                  "deferred_steps": len(pending), "actor_selector_calls": 0})


class ToolSequenceTool(BaseAgenticTool):
    name = "tool_sequence"
    description = ("Execute already-decided literal tool steps with exact JSON receipt guards in one call. "
                   "Useful for known dependent commands or workflows. All child tools retain native authorization. "
                   "Unknown results, errors or new parent input return control; never guess future arguments.")
    input_schema = ToolSequenceInput
    is_read_only = True  # The wrapper grants no authority; every child is independently authorized.
    risk_level = "low"

    def is_concurrency_safe_call(self, **kwargs):
        return False

    async def execute(self, **kwargs):
        raise ValueError("Tool sequences require the native authorized executor")
