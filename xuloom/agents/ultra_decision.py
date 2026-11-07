"""Recovery compatibility for Ultra plans saved before direct native delegation."""
import copy
import hashlib
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator
from xuloom.core.messages import create_tool_result_message, get_msg_uuid
from xuloom.core.run_result import PersistenceError


def request_messages(engine, messages):
    """Omit obsolete routing instructions from requests; keep stored history intact."""
    result = []
    for message in messages:
        content = message.get('content')
        if message.get('_is_meta'):
            if message.get('_origin') == 'tool_protocol' and isinstance(content, str) and content.startswith('[Ultra decision rejected before any tool execution]'):
                continue
            if message.get('role') == 'assistant' and isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text' and isinstance(content[0].get('text'), str) and content[0]['text'].startswith('Collaboration decision: '):
                continue
        result.append(message)
    return result


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
