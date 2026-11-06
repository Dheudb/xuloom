"""One model-owned Ultra decision over the normal request and worker journals."""
import copy
import hashlib
from types import SimpleNamespace
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator, ValidationError
from xuloom.core.messages import create_tool_result_message, get_msg_uuid
from xuloom.core.run_result import PersistenceError
from xuloom.protocols.dsml import ToolProtocolError, parse_tool_arguments
from xuloom.protocols.stream_watch import StreamInterrupted


class Assignment(BaseModel):
    model_config = {"extra": "forbid"}
    prompt: str = Field(min_length=1)
    description: str = Field(min_length=1)
    subagent_type: Optional[str] = None


class Decision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["answer", "delegate", "sequential"]
    reason: str = ""
    answer: str = ""
    main_task: str = ""
    workers: list[Assignment] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.decision == "answer":
            if not self.answer.strip() or self.workers or self.main_task:
                raise ValueError("answer requires a final answer and no work assignments")
        elif not self.reason.strip() or not self.main_task.strip() or self.answer:
            raise ValueError("work requires a reason and main_task, without a final answer")
        elif (self.decision == "delegate") != bool(self.workers):
            raise ValueError("delegate requires meaningful workers; sequential requires none")
        if any(not row.prompt.strip() or not row.description.strip() for row in self.workers):
            raise ValueError("Worker assignments must not be blank")
        return self


def pending(engine):
    return (engine.collaboration_mode == "ultra" and engine.agent_depth == 0
        and not engine.tool_context.get("delegation")
        and not engine.tool_context.get("collaboration_run", {}).get("decision"))


def calls_for(plan, owner):
    if not isinstance(owner, str) or not owner:
        raise ValueError("Ultra decisions require a durable original task identity")
    calls = []
    for index, worker in enumerate(plan.workers):
        identity = "ultra-" + hashlib.sha256(owner.encode()).hexdigest()[:16] + "-" + str(index)
        calls.append({"type":"tool_use", "id":identity, "name":"spawn_agent",
            "input":{**worker.model_dump(exclude_none=True), "request_id":identity}})
    return calls


def receipts_saved(engine, results):
    decision = engine.tool_context.get("collaboration_run", {}).get("decision") or {}
    expected = {call["id"] for call in decision.get("calls", [])}
    resolved = {result.get("tool_use_id") for result in results}
    resolved.update(block.get("tool_use_id") for message in engine.messages for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result")
    if expected and expected <= resolved:
        decision["receipts_saved"] = True


def contract(engine):
    roles = sorted(engine.tool_context["agent_registry"].definitions())
    return ("\n# Ultra collaboration decision (this request only)\n"
        'Return one JSON object, no tool calls or prose: {"decision":"answer|delegate|sequential",'
        '"reason":"brief decision basis","answer":"","main_task":"","workers":'
        '[{"description":"label","prompt":"self-contained independent assignment","subagent_type":"registered role"}]}. '
        "For a simple conversational task, choose answer and put the complete reply in answer; omit workers/main_task. "
        "For complex work, delegate useful independent research, alternatives or checks; keep the critical path and synthesis in main_task. "
        "Do not invent workers to meet a quota. Choose sequential with a brief reason when no useful independent branch exists. "
        "Include applicable user/Skill constraints in assignments. The runtime launches accepted workers and supplies receipts; do not claim launches here. "
        "Roles (or omit subagent_type): " + ", ".join(roles))


def decision_callback(engine, callback):
    async def emit(event):
        if event.get("type") in {"stream_status", "model_stream", "model_status", "model_retry"}:
            await engine._emit_stream_event(callback, event)
    return emit


async def receive(engine, deciding, *, event_callback=None, **kwargs):
    if not deciding:
        return await engine._stream_model_response(event_callback=event_callback, **kwargs)
    engine.tool_context["ultra_deciding"] = True
    try:
        response, entries, fallback = await engine._stream_model_response(
            event_callback=decision_callback(engine, event_callback), **kwargs)
        if entries:
            await engine._cancel_streaming_tool_tasks(entries)
        return response, [], fallback
    except ToolProtocolError as error:
        error.code, error.visible_text = "invalid_collaboration_decision", ""
        raise
    except StreamInterrupted as error:
        error.partial_text = ""  # An incomplete decision is not a user answer.
        raise
    finally:
        engine.tool_context.pop("ultra_deciding", None)


def convert(engine, response, user_message):
    raw = "\n".join(engine._tb_get(block, "text", "") for block in response.content
        if engine._tb_get(block, "type", "") == "text")
    try:
        if response.stop_reason != "end_turn" or any(engine._tb_get(b, "type", "") not in {"text", "thinking", "redacted_thinking"} for b in response.content):
            raise ValueError("Return a complete JSON decision, not executable tool calls")
        text = raw.strip()
        if text.startswith("```json\n") and text.endswith("```"): text = text[8:-3].strip()
        plan = Decision.model_validate(parse_tool_arguments(text))
        roles = engine.tool_context["agent_registry"].definitions()
        for worker in plan.workers:
            if worker.subagent_type is not None and worker.subagent_type not in roles:
                raise ValueError("Unknown worker role; registered roles: " + ", ".join(sorted(roles)))
        if plan.decision == "answer" and engine.tool_context["workflow_completion"].pending:
            raise ValueError("The selected Skill requires execution; choose delegate or sequential")
        if plan.workers and "spawn_agent" not in engine._get_effective_tools():
            raise ValueError("Delegation is unavailable in the current role/mode")
    except (ValueError, ValidationError) as error:
        detail = error.errors(include_input=False, include_context=False, include_url=False) if isinstance(error, ValidationError) else str(error)
        raise ToolProtocolError("Invalid Ultra decision: " + str(detail), code="invalid_collaboration_decision", raw_text=raw) from None
    owner = get_msg_uuid(user_message)
    calls = calls_for(plan, owner)
    engine.tool_context["collaboration_run"]["decision"] = {"user_message_id": owner, **plan.model_dump(), "calls": calls}
    thinking = [block for block in response.content if engine._tb_get(block, "type", "") in {"thinking", "redacted_thinking"}]
    content = thinking + ([engine._make_text_block(plan.answer)] if plan.decision == "answer" else
        [engine._make_text_block("Collaboration decision: " + plan.reason + "\nMain assignment: " + plan.main_task)] +
        [engine._make_tool_use_block(tool_id=c["id"], name=c["name"], input_payload=c["input"]) for c in calls])
    return SimpleNamespace(content=content, stop_reason="tool_use" if calls else "end_turn", usage=response.usage), plan


async def recover(engine, user_message, *, event_callback=None, sys_print_callback=print):
    """Fill only missing native receipts after explicit user resume; never replay uncertain effects."""
    decision = engine.tool_context.get("collaboration_run", {}).get("decision") or {}
    if not decision: return
    if decision.get("user_message_id") != get_msg_uuid(user_message):
        raise ValueError("Saved Ultra decision belongs to a different task")
    if decision.keys() - (Decision.model_fields.keys() | {"user_message_id", "calls", "receipts_saved"}):
        raise ValueError("Invalid saved Ultra decision fields")
    plan = Decision.model_validate({key:decision[key] for key in Decision.model_fields if key in decision})
    calls = calls_for(plan, decision["user_message_id"])
    if calls != decision.get("calls") or type(decision.get("receipts_saved", False)) is not bool:
        raise ValueError("Saved Ultra calls do not match their original decision")
    if decision.get("receipts_saved"): return
    if not calls: return
    originals = [message for message in engine.messages if message.get("role") == "assistant"
        and any(block.get("id") == calls[0]["id"] for block in message.get("content", []) if isinstance(block, dict))]
    if not originals: raise PersistenceError("Saved Ultra decision has no matching assistant execution record")
    resolved = {block.get("tool_use_id") for message in engine.messages for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"}
    missing = [call for call in calls if call["id"] not in resolved]
    if not missing:
        receipts_saved(engine, [])
        return
    results = await engine._execute_tool_batches(copy.deepcopy(missing), sys_print_callback, event_callback=event_callback)
    # Preserve original tool pairing order; no second execution registry.
    parent = originals[0]
    paired = next((m for m in engine.messages if m.get("_source_assistant_uuid") == get_msg_uuid(parent)), None)
    if paired is None:
        engine.messages.insert(engine.messages.index(parent) + 1,
            create_tool_result_message(results, source_assistant_uuid=get_msg_uuid(parent)))
    else:
        paired["content"].extend(results)
    receipts_saved(engine, results)
    engine._sync_message_context()
    if not engine.persist_session_state(): raise PersistenceError("Ultra recovery receipts could not be saved")
    if any(result.get("is_error") for result in results):
        raise ValueError("Ultra worker launch could not be confirmed; inspect the saved task receipts before continuing")
