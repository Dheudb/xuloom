"""Native, receipt-guarded continuations at the existing reasoning boundary.

The actor authors literal actions; this module never invents arguments or calls
another model. All effects go through the existing authorized tool executor.
"""
import asyncio
import copy
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field
from xuloom.core.messages import create_assistant_message, create_tool_result_message
from xuloom.core.run_result import PersistenceError
from xuloom.protocols.dsml import ToolProtocolError
from xuloom.tools.tool_sequence import LiteralStep, digest, observation

FIELD = "__xuloom_continuation"
KEY = "native_continuation"
PROTOCOL = ("# Native continuation protocol\nNormal tool arguments optionally accept __xuloom_continuation:{nodes:[{id,tool,arguments,depends_on,when}]}. "
    "Use it when useful future actions and all literal arguments are already determined; do not wait for another actor call to restate them. "
    "depends_on defaults to ['root'] (the current call); use earlier node IDs for dependencies. "
    "when defaults to []; each condition is {step:'root' or ancestor ID,path:['JSON','keys'],equals:literal JSON value}. "
    "All conditions must match complete native receipts. False branches are skipped; missing evidence returns control. "
    "Use available tools, unique IDs and non-recursive literal arguments. Omit the field or use nodes:[] when new interpretation is needed. "
    "Do not duplicate declared nodes as separate calls. Native permissions, cancellation and acceptance rules remain unchanged.")


class Decision(LiteralStep):
    depends_on: list[str] = Field(default_factory=lambda: ["root"], min_length=1, max_length=128)


class Continuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nodes: list[Decision] = Field(max_length=128)


def schema(definition):
    """Optional metadata belongs to the harness, not to native tool arguments."""
    result = copy.deepcopy(definition)
    body = result.get("input_schema", {})
    if body.get("type") != "object" or FIELD in body.get("properties", {}) or result.get("name") == "tool_sequence":
        return result
    # Detailed semantics are stated once in the shared tool protocol, rather
    # than repeating a large recursive schema for every active native/MCP tool.
    body.setdefault("properties", {})[FIELD] = {"type": "object", "properties": {
        "nodes": {"type": "array", "items": {"type": "object"}, "maxItems": 128}},
        "required": ["nodes"], "additionalProperties": False,
        "description": "Optional literal action graph; see the shared native continuation protocol."}
    return result


def binding(engine):
    sandbox = engine.sandbox_manager.active_sandbox or {}
    return digest([engine.session_id, engine.agent_id, engine.session_cwd, engine.model_provider,
        engine.primary_model, engine.api_base_url, engine._api_key, engine.get_mode(),
        engine.execution_sandbox.mode, sandbox.get("path"), engine.agent_role, engine.permission_manager.approval_policy.mode,
        [m.get("_uuid") for m in engine.messages if m.get("role") == "user" and not m.get("_is_tool_result")
         and m.get("_origin") != "resume_control"]])


def save(engine):
    if not engine.persist_session_state():
        raise PersistenceError("Could not save native continuation; no further action was authorized. " + engine.session_manager.last_save_error)


def contract(engine, name):
    handler = engine._get_effective_tools().get(name)
    return digest(handler.get_tool_definition()) if handler is not None else None


def current(engine, graph, name=None):
    mailbox = engine.tool_context.get("delegated_mailbox")
    if (engine.abort_event.is_set() or graph["binding"] != binding(engine) or callable(mailbox) and mailbox()
            or getattr(engine, "_continuation_approval_revision", engine.permission_manager.approval_policy.revision) != engine.permission_manager.approval_policy.revision):
        return False
    if name is None:
        return True
    snapshot = getattr(engine, "_continuation_handlers", {}).get(name)
    handler = engine._get_effective_tools().get(name)
    return contract(engine, name) == graph["contracts"].get(name) and (snapshot is None or snapshot is handler)


def validate(engine, nodes):
    known, ancestors = {"root"}, {"root": set()}
    for node in nodes:
        if node.id in known or any(dep not in known for dep in node.depends_on) or node.tool == "tool_sequence" or FIELD in node.arguments:
            raise ToolProtocolError("Continuation requires unique IDs, earlier dependencies and literal non-recursive actions")
        reachable = set(node.depends_on)
        for dep in node.depends_on:
            reachable.update(ancestors[dep])
        if any(guard.step not in reachable or any(not key for key in guard.path) for guard in node.when):
            raise ToolProtocolError("Continuation guards must reference ancestors using nonempty object keys")
        handler = engine._get_effective_tools().get(node.tool)
        if handler is None:
            raise ToolProtocolError("Continuation tools must already be available; discover them first")
        engine.execution_sandbox.check_tool(node.tool)
        normalized = node.arguments
        if getattr(handler, "input_schema", None) is not None:
            if set(normalized) - set(handler.input_schema.model_fields):
                raise ToolProtocolError("Unknown continuation tool arguments")
            normalized = handler.input_schema.model_validate(normalized, strict=True).model_dump()
        validator = getattr(handler, "validate_input", None)
        error = validator(**normalized) if callable(validator) else None
        if isinstance(error, str) and error:
            raise ToolProtocolError("Invalid continuation arguments: " + error)
        digest(node.arguments)
        known.add(node.id)
        ancestors[node.id] = reachable


def register(engine, block, declaration):
    plan = Continuation.model_validate(declaration, strict=True)
    if not plan.nodes:
        return
    if engine.tool_context.get(KEY, {}).get("status") == "active":
        raise ToolProtocolError("A native continuation is already active; do not overwrite its pending actions")
    validate(engine, plan.nodes)
    root = {"id": engine._tb_get(block, "id"), "name": engine._tb_get(block, "name"),
            "input": {key: value for key, value in engine._tb_get(block, "input", {}).items() if key != FIELD}}
    names = {root["name"], *(node.tool for node in plan.nodes)}
    graph = {"version": 1, "id": root["id"], "root": root, "plan": plan.model_dump(),
        "plan_hash": digest(plan.model_dump()), "binding": binding(engine),
        "contracts": {name: contract(engine, name) for name in names}, "status": "active", "reason": "root_pending",
        "states": {"root": "pending", **{node.id: "pending" for node in plan.nodes}},
        "observations": {}, "receipts": {}, "certificates": []}
    if len(json.dumps(graph, ensure_ascii=False).encode()) > 262144:
        raise ToolProtocolError("Continuation declaration is too large; keep the original native tool workflow")
    engine.tool_context[KEY] = graph
    engine._continuation_handlers = {name: engine._get_effective_tools().get(name) for name in names}
    engine._continuation_approval_revision = engine.permission_manager.approval_policy.revision


def call_id(graph, node_id):
    return "continuation-" + digest([graph["id"], graph["plan_hash"], node_id])[:40]


def observe(engine, tool_id, raw, receipt):
    """Updated in the same checkpoint as the native tool's durable receipt."""
    graph = engine.tool_context.get(KEY)
    if not graph or graph.get("status") != "active":
        return
    node_id = "root" if tool_id == graph["id"] else next((node["id"] for node in graph["plan"]["nodes"] if call_id(graph, node["id"]) == tool_id), None)
    if node_id is None:
        # An unrelated sibling could invalidate a predeclared workflow.
        graph.update(status="deferred", reason="unrelated_tool_executed")
        return
    graph["receipts"][node_id] = digest(receipt)
    value = observation(raw)
    try:
        if len(json.dumps(value, allow_nan=False).encode()) > 65536:
            value = None
    except (ValueError, TypeError):
        value = None
    graph["observations"][node_id] = copy.deepcopy(value)
    row = next((row for row in engine.recent_tool_activity if row.get("tool_use_id") == tool_id), None)
    if row is not None:
        row["continuation_observation_hash"] = digest(value)
    graph["states"][node_id] = "failed" if receipt.get("is_error") else "completed"
    if receipt.get("is_error"):
        graph.update(status="deferred", reason="tool_failed")
    name = graph["root"]["name"] if node_id == "root" else next(node["tool"] for node in graph["plan"]["nodes"] if node["id"] == node_id)
    if name == "command_tool" and (not isinstance(value, dict) or value.get("status") != "completed" or type(value.get("exit_code")) is not int or value["exit_code"] != 0):
        graph.update(status="deferred", reason="command_not_successful")


def restore(engine, value):
    engine.tool_context.pop(KEY, None)
    engine._continuation_handlers = {}
    engine._continuation_approval_revision = engine.permission_manager.approval_policy.revision
    if not value:
        return
    graph = copy.deepcopy(value)
    try:
        if (not isinstance(graph, dict)
                or any(not isinstance(graph.get(key), dict) for key in ("root", "plan", "states", "contracts", "receipts", "observations"))
                or not isinstance(graph.get("certificates"), list)
                or graph.get("status") not in {"active", "deferred", "exhausted"}
                or any(not isinstance(graph.get(key), str) for key in ("id", "binding", "plan_hash"))):
            raise ValueError("Malformed saved continuation")
        plan = Continuation.model_validate(graph["plan"], strict=True)
        def require(condition):
            if not condition:
                raise ValueError("Saved continuation is inconsistent with native execution evidence")
        require(graph["version"] == 1 and digest(plan.model_dump()) == graph["plan_hash"])
        expected = {"root", *(node.id for node in plan.nodes)}
        require(set(graph["states"]) == expected and all(state in {"pending", "running", "completed", "skipped", "failed"} for state in graph["states"].values()))
        require(set(graph["contracts"]) == {graph["root"]["name"], *(node.tool for node in plan.nodes)}
            and all(isinstance(value, str) and len(value) == 64 for value in graph["contracts"].values()))
        # Bind to the actual declaration, not a detached saved graph.
        roots = [block for message in engine.messages if message.get("role") == "assistant" for block in message.get("content", [])
                 if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == graph["id"]]
        require(len(roots) == 1 and roots[0]["name"] == graph["root"]["name"])
        require(digest(Continuation.model_validate(roots[0]["input"].get(FIELD), strict=True).model_dump()) == graph["plan_hash"])
        require({key: val for key, val in roots[0]["input"].items() if key != FIELD} == graph["root"]["input"])
        validate(engine, plan.nodes)
        for node_id, state in graph["states"].items():
            if state == "completed":
                identity = graph["id"] if node_id == "root" else call_id(graph, node_id)
                rows = [row for row in engine.recent_tool_activity if row.get("tool_use_id") == identity]
                require(len(rows) == 1 and rows[0].get("status") == "ok" and digest(rows[0]["tool_result"]) == graph["receipts"][node_id])
                require(rows[0].get("continuation_observation_hash") == digest(graph["observations"][node_id]))
        if any(state == "running" for state in graph["states"].values()) or graph["states"]["root"] == "pending":
            graph.update(status="deferred", reason="recovery_uncertain")
        elif graph.get("status") == "active" and not current(engine, graph):
            graph.update(status="deferred", reason="scope_changed_on_restore")
    except (ValueError, TypeError, KeyError, PermissionError, ToolProtocolError):
        graph = {"version": 1, "status": "deferred", "reason": "invalid_saved_continuation"}
    engine.tool_context[KEY] = graph


def reconcile_messages(engine, graph):
    """Close the save-after-execution/before-message gap without replaying effects."""
    identities = {graph["id"], *(call_id(graph, node["id"]) for node in graph["plan"]["nodes"])}
    present = {block.get("tool_use_id") for message in engine.messages for block in message.get("content", [])
               if isinstance(block, dict) and block.get("type") == "tool_result"}
    for message in list(engine.messages):
        blocks = [block for block in message.get("content", []) if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in identities and block.get("id") not in present]
        receipts = []
        for block in blocks:
            row = next((row for row in engine.recent_tool_activity if row.get("tool_use_id") == block["id"] and row.get("tool_result")), None)
            if row:
                receipts.append(copy.deepcopy(row["tool_result"]))
                present.add(block["id"])
        if receipts:
            engine.messages.append(create_tool_result_message(receipts, source_assistant_uuid=message["_uuid"]))


def evaluate(guard, observations):
    value = observation(observations.get(guard.step))
    if guard.path[:2] == ["output", "text"] and (not isinstance(value, dict) or not isinstance(value.get("output"), dict)
            or value["output"].get("eof") is not True or value["output"].get("has_more") is not False):
        return None
    for key in guard.path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return digest(value) == digest(guard.equals)


async def advance(engine, *, turn, event_callback, sys_print_callback):
    """Run before a registered slow request. No model/selector calls occur here."""
    graph = engine.tool_context.get(KEY)
    if not graph or graph.get("status") != "active":
        return
    reconcile_messages(engine, graph)
    nodes = Continuation.model_validate(graph["plan"], strict=True).nodes
    boundary = "native-boundary-" + str(uuid.uuid4())
    async def defer(reason):
        graph.update(status="deferred", reason=reason)
        await engine._record_loop_transition("continuation_deferred", turn=turn, event_callback=event_callback, graph_id=graph["id"], boundary_id=boundary, detail=reason)
        save(engine)
    while graph["status"] == "active":
        if engine.goal:
            from xuloom.features.goals import pending_control
            await pending_control(engine)
        if engine.abort_event.is_set():
            raise asyncio.CancelledError()
        if not current(engine, graph):
            return await defer("new_input_or_scope_changed")
        if any(state == "running" for state in graph["states"].values()) or graph["states"]["root"] != "completed":
            return await defer("recovery_uncertain")
        ready = []
        for node in nodes:
            if graph["states"][node.id] != "pending":
                continue
            predecessors = [graph["states"][dep] for dep in node.depends_on]
            if any(state in {"skipped", "failed"} for state in predecessors):
                graph["states"][node.id] = "skipped"
                continue
            if not all(state == "completed" for state in predecessors):
                continue
            checks = [evaluate(guard, graph["observations"]) for guard in node.when]
            if None in checks:
                return await defer("guard_unknown")
            if not all(checks):
                graph["states"][node.id] = "skipped"
                continue
            ready.append(node)
        if not ready:
            graph.update(status="exhausted", reason="declared_actions_finished")
            save(engine)
            break
        # Multiple eligible non-read-only actions require a semantic choice.
        # Deterministic fan-out is allowed only for genuinely independent safe tools.
        if len(ready) > 1 and not all(engine._is_concurrency_safe(node.tool, node.arguments)
                and engine.permission_manager._is_read_only(engine._get_effective_tools()[node.tool], node.arguments) for node in ready):
            return await defer("ambiguous_candidates")
        for node in ready:
            if not current(engine, graph, node.tool):
                return await defer("tool_or_scope_changed")
        blocks = [{"type": "tool_use", "id": call_id(graph, node.id), "name": node.tool, "input": copy.deepcopy(node.arguments)} for node in ready]
        message = create_assistant_message(blocks, model=engine.primary_model, stop_reason="tool_use")
        message["_origin"] = "native_continuation"
        engine.messages.append(message)
        for node in ready:
            graph["states"][node.id] = "running"
        engine._sync_message_context()
        save(engine)
        async def run(node, block):
            internal = {**block, "_literal_input": True, "_continuation_guard": graph["id"]}
            receipt = await engine._execute_tool_wrapper(internal, sys_print_callback, turn=turn, event_callback=event_callback)
            return node, receipt
        # Native request concurrency bound; Auto/Ultra do not alter graph semantics.
        semaphore = asyncio.Semaphore(engine.MAX_TOOL_CONCURRENCY)
        async def bounded(node, block):
            async with semaphore:
                return await run(node, block)
        tasks = [asyncio.create_task(bounded(node, block)) for node, block in zip(ready, blocks)]
        try:
            completed = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        receipts = [receipt for _, receipt in completed]
        engine.messages.append(create_tool_result_message(receipts, source_assistant_uuid=message["_uuid"]))
        for node, receipt in completed:
            if graph["states"].get(node.id) != "completed":
                continue
            certificate = {"graph_hash": graph["plan_hash"], "node_id": node.id,
                "boundary_id": boundary, "call_id": call_id(graph, node.id),
                "root_action_hash": digest(graph["root"]), "scope_hash": graph["binding"],
                "action_hash": digest([node.tool, node.arguments]), "receipt_hash": digest(receipt),
                "predecessor_receipts": {dep: graph["receipts"][dep] for dep in node.depends_on},
                "reasoning_request_issued": False, "selector_requests": 0}
            graph["certificates"].append(certificate)
            await engine._record_loop_transition("continuation_substitution", turn=turn, event_callback=event_callback, **certificate)
        engine._sync_message_context()
        save(engine)
