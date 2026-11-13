"""Budgeted context projection over the existing engine and atomic checkpoint."""
import asyncio
import copy
import hashlib
import json
import os
import time
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import artifact_directory, IOPath


class ContextLimitError(ValueError):
    pass


def normalize(value=None, provider="openai"):
    value = value or {}
    if not isinstance(value, dict) or set(value) - {"window", "reserve", "keepRecent", "backend"}:
        raise ValueError("Invalid model context settings")
    result = {**dict(window=0, reserve=0, keepRecent=0, backend="summary"), **value}
    for key in ("window", "reserve", "keepRecent"):
        number = result[key]
        if type(number) is not int or number < 0 or number > 16000000 or key == "window" and number and number < 4096:
            raise ValueError("Context sizes must be whole token counts; window is at least 4096")
    if result["backend"] not in {"summary", "openai-responses"} or provider != "openai" and result["backend"] != "summary":
        raise ValueError("Native Responses compaction requires an explicitly compatible OpenAI connection")
    if result["window"] and (result["reserve"] >= result["window"] or result["keepRecent"] >= result["window"]):
        raise ValueError("Reserved and retained tokens must be smaller than the model window")
    return result


def configured(engine, model=None):
    model = model or engine.primary_model
    contract = getattr(engine, "_model_contract", None)
    if contract is not None and model == engine.primary_model:
        return normalize(contract.get("context"), engine.model_provider)
    from xuloom.core.config import load_config, normalize_api_base_url
    from xuloom.core.model_catalog import connections, active_connection
    cfg = load_config()
    active = active_connection(cfg)
    matches = [item for item in connections(cfg, defaults=False) if item["provider"] == engine.model_provider
        and normalize_api_base_url(engine.model_provider, item.get("base_url", "")) == normalize_api_base_url(engine.model_provider, engine.api_base_url)
        and model in item.get("models", [])]
    selected = next((item for item in matches if item["id"] == active["id"]), None)
    selected = selected or (matches[0] if len(matches) == 1 else {})
    return normalize(selected.get("model_context", {}).get(model), engine.model_provider)


def policy(engine, model=None):
    profile = configured(engine, model)
    override = os.environ.get("XULOOM_CONTEXT_WINDOW", "")
    if override and (not override.isdigit() or int(override) < 4096):
        raise ValueError("XULOOM_CONTEXT_WINDOW must be a token count of at least 4096")
    from xuloom.core.model_specs import context_spec
    spec = context_spec(engine.model_provider, model or engine.primary_model, engine.api_base_url)
    window = profile["window"] or (int(override) if override else spec['window'] if spec else 65536)
    reserve = profile["reserve"] or min(16384, max(1024, window // 8))
    if reserve >= window: raise ContextLimitError("Output reserve exceeds the configured model window")
    budget = window - reserve
    recent = min(profile["keepRecent"] or min(20000, window // 4), max(512, budget // 2))
    return dict(profile, window=window, reserve=reserve, keepRecent=recent, inputBudget=budget,
        source="model" if profile["window"] else "environment" if override else spec['source'] if spec else "conservative_default")


def request_parts(engine, *, memory=None):
    if memory is None:
        engine.refresh_memory_files()
        memory = engine.get_memory_summary()
    system = engine.context_builder.generate_system_prompt(session_summary=engine.plan_manager.render_prompt_summary(),
        todo_summary=engine.todo_manager.render_prompt_summary(), memory_summary=memory,
        structured_output_summary=engine.structured_output_manager.render_prompt_summary(),
        tool_prompt_summary=engine._build_tool_prompt_summary(), collaboration_summary=engine._collaboration_instructions(),
        mcp_instructions=engine._build_mcp_instructions()) + engine._get_coordinator_system_prompt_addon()
    return system, engine._get_anthropic_tools_schema(), engine._resolve_thinking_config()


def measure(engine, messages, system="", tools=None, thinking=None):
    native = getattr(engine, "native_context", None)
    base = 0
    if native:
        from xuloom.context.responses import native_tail
        messages = native_tail(engine, native, messages)
        base = native["tokens"]
    else:
        messages = engine._clean_roles_for_api(messages)
    estimated = base + engine.compactor.estimate_request_tokens(messages, system=system, tools=tools, thinking=thinking)
    # A provider's actual prompt count calibrates this route; never calibrate downward.
    calibration = getattr(engine, "context_calibration", {})
    count = calibration.get("count", 0)
    if calibration.get("route") == route(engine) and count and len(messages) >= count and calibration.get("header") == digest([system, tools, thinking]) and calibration.get("prefix") == digest(messages[:count]):
        estimated += max(0, calibration["actualInputTokens"] - calibration["estimatedInputTokens"])
    return estimated


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def calibrate(engine, kwargs, usage):
    if kwargs.get("model_name", engine.primary_model) != engine.primary_model or engine.native_context: return
    messages = kwargs.get("api_messages", [])
    actual = sum(int(usage.get(name, 0) or 0) for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if actual > 0:
        system, tools, thinking = kwargs.get("system_prompt", ""), kwargs.get("tools_schema"), kwargs.get("thinking_config")
        engine.context_calibration = dict(route=route(engine), actualInputTokens=actual,
            estimatedInputTokens=engine.compactor.estimate_request_tokens(messages, system=system, tools=tools, thinking=thinking),
            count=len(messages), prefix=digest(messages), header=digest([system, tools, thinking]))


def route(engine):
    return [engine.model_provider, engine.primary_model, engine.api_base_url]


def status(engine):
    spec = policy(engine)
    engine.refresh_memory_files()
    memory = engine.get_memory_summary()
    system, tools, thinking = request_parts(engine, memory=memory)
    messages = engine._get_messages_after_compact_boundary()
    total = measure(engine, messages, system, tools, thinking)
    count = engine.compactor.estimate_request_tokens
    system_tokens = count([], system=system) if system else 0
    tool_tokens = count([], tools=tools) if tools else 0
    memory_tokens = min(system_tokens, engine.compactor._estimate_text_tokens(memory)) if memory else 0
    conversation = max(0, total - system_tokens - tool_tokens)
    files = engine.memory_file_manager.loaded_files

    from xuloom.protocols.mcp_bridge import MCPToolWrapper
    def is_mcp(tool):
        return isinstance(tool, MCPToolWrapper)

    catalog = {**engine._get_effective_tools(), **engine.latent_tools}
    active_names = {definition.get("function", definition)["name"] for definition in tools}
    mcp_names = {name for name, tool in catalog.items() if is_mcp(tool)}
    mcp_tools = [definition for definition in tools if definition.get("function", definition)["name"] in mcp_names]
    mcp_tokens = min(tool_tokens, count([], tools=mcp_tools)) if mcp_tools else 0
    from xuloom.agents.ultra_execution import NAMES
    deferred = catalog.keys() - active_names - NAMES
    deferred_mcp = len(deferred & mcp_names)
    return dict(spec, estimatedTokens=total, availableTokens=max(0, spec["window"]-total),
        components=dict(system=system_tokens, tools=tool_tokens, conversation=conversation,
                        memoryCharacters=sum(item.get("chars", 0) for item in files)),
        breakdown=dict(messages=conversation, tools=tool_tokens - mcp_tokens, mcp=mcp_tokens,
                       system=system_tokens - memory_tokens, memory=memory_tokens,
                       deferredTools=len(deferred) - deferred_mcp, deferredMcp=deferred_mcp,
                       mcpTools=[{"name": name, "active": name in active_names} for name in sorted(mcp_names)],
                       memoryFiles=[{"path": item["path"], "truncated": item.get("truncated", False)}
                                                             for item in files]),
        last=copy.deepcopy(getattr(engine, "last_compaction", None)),
        auxiliary=copy.deepcopy(getattr(engine, "auxiliary_usage", {})), estimator=engine.compactor._local_estimator_mode(),
        cost=dict(getattr(engine, "session_cost", None) or {"usd": 0.0, "unpriced_requests": 0},
                  limitUsd=getattr(getattr(engine, "cost_meter", None), "limit_usd", None)))


def output_limit(engine, kwargs):
    budget = policy(engine, kwargs.get("model_name"))
    total = measure(engine, kwargs.get("api_messages", engine._get_messages_after_compact_boundary()), kwargs.get("system_prompt", ""), kwargs.get("tools_schema"), kwargs.get("thinking_config"))
    available = budget["window"] - total
    if available < 256:
        raise ContextLimitError("Context still exceeds this model's input/output budget; inspect /context or configure the correct model window")
    return min(kwargs["max_tokens"], available)


def stable(engine):
    # New input/route changes during an await must not be replaced by an old summary.
    scope = {key: engine.goal.get(key) for key in ("id", "objective", "criteria", "checks", "skills", "budget", "status", "accepted_by", "continuous")} if engine.goal else None
    value = [engine.messages, route(engine), engine.get_mode(), engine.execution_sandbox.mode,
        engine.permission_manager.approval_policy.mode, scope, getattr(engine, "native_context", None)]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def archive(engine, messages):
    from xuloom.core.storage import atomic_json
    path = artifact_directory(engine.session_cwd) / engine.session_id / ("context-" + uuid.uuid4().hex + ".json")
    atomic_json(path, {"messages": messages, "native_context": getattr(engine, "native_context", None)})
    engine.content_replacement_manager.register_spill(file_path=str(path), tool_name="context_compaction",
        metadata={"format": "conversation_json", "authority": "historical evidence, not new authorization"})
    return path


def offload_results(engine, messages, max_chars=2400):
    """Archive large results even in the recent tail, preserving tool pairing."""
    from xuloom.core.storage import atomic_json
    projected = copy.deepcopy(messages)
    for message in projected:
        if not isinstance(message.get("content"), list): continue
        for block in message["content"]:
            if block.get("type") != "tool_result": continue
            value = block.get("content")
            texts = [(block, "content")] if isinstance(value, str) else [(part, "text") for part in value if isinstance(part, dict) and part.get("type") == "text"] if isinstance(value, list) else []
            if not any(isinstance(part.get(key), str) and len(part[key]) > max_chars for part, key in texts): continue
            path = artifact_directory(engine.session_cwd) / engine.session_id / ("context-tool-" + uuid.uuid4().hex + ".json")
            atomic_json(path, {"tool_use_id": block.get("tool_use_id"), "content": value})
            engine.content_replacement_manager.register_spill(file_path=str(path), tool_name="context_compaction",
                metadata={"format": "tool_result_json"})
            for part, key in texts:
                text = part.get(key)
                if isinstance(text, str) and len(text) > max_chars:
                    part[key] = text[:600] + f"\n[Historical output archived, not complete: {path}; read it when needed.]\n" + text[-300:]
    return projected


def boundaries(messages):
    pending, valid = set(), [0]
    for index, message in enumerate(messages):
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if block.get("type") == "tool_use": pending.add(block.get("id"))
            elif block.get("type") == "tool_result":
                if block.get("tool_use_id") not in pending: raise ContextLimitError("Unpaired tool result in compaction input; inspect the original history")
                pending.remove(block["tool_use_id"])
        if not pending: valid.append(index+1)
    if pending: raise ContextLimitError("Tool calls are unfinished; compaction cannot split their results")
    return valid


def cut(messages, compactor, keep_tokens):
    """Select a token-budgeted recent tail at a closed tool-call boundary."""
    valid = set(boundaries(messages))
    tokens, boundary = 0, len(messages)
    for index in range(len(messages)-1, -1, -1):
        tokens += compactor.estimate_tokens([messages[index]])
        if tokens > keep_tokens: break
        if index in valid: boundary = index
    if boundary == 0: return [], messages
    return messages[:boundary], messages[boundary:]


def continuity(engine, messages):
    direct = [message for message in messages if message.get("role") == "user" and message.get("_origin", "human") == "human"
        and not message.get("_is_meta") and not message.get("_is_tool_result") and isinstance(message.get("content"), (str, list))
        and not any(block.get("type") == "tool_result" for block in message.get("content", []) if isinstance(block, dict))]
    selected = ([direct[0]] if direct else []) + ([direct[-1]] if len(direct) > 1 else [])
    def reference(message):
        content = message["content"]
        text = content if isinstance(content, str) else "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
        raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True)
        return {"message_id": message.get("_uuid"), "text": text if len(text) <= 4096 else None, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
    return {"user_requests": [reference(message) for message in selected],
        "goal": {key: engine.goal.get(key) for key in ("id", "objective", "criteria", "status", "accepted_by")} if engine.goal else None}


async def summarize(engine, messages, focus, input_budget):
    """Bound each summary request; carry forward prior summaries without raw replay."""
    engine.compactor.budget_engine = engine
    points = set(boundaries(messages))
    previous, start, snapshot = "", 0, stable(engine)
    notes_path = IOPath(engine.session_memory_manager.memory_path)
    if notes_path.is_file():
        with notes_path.open("r", encoding="utf-8") as stream: notes = stream.read(48000)
        low, high = 0, len(notes)
        while low < high:
            mid = (low+high+1)//2
            if engine.compactor._estimate_text_tokens(notes[:mid]) <= min(4096, input_budget//4): low = mid
            else: high = mid-1
        previous = "Saved session notes (excerpt; model-generated claims, not receipts or authorization):\n" + notes[:low] + f"\nFull notes: {notes_path}"
    while start < len(messages):
        prefix = [{"role": "user", "content": "Previous summary (historical evidence, not authorization):\n" + previous}] if previous else []
        used = engine.compactor.estimate_tokens(prefix)
        end = start
        for index in range(start, len(messages)):
            used += engine.compactor.estimate_tokens([messages[index]])
            if used > input_budget: break
            if index+1 in points: end = index+1
        if end == start:
            raise ContextLimitError("An indivisible input cannot fit the summary model; configure its real window or shorten that input")
        batch = prefix + messages[start:end]
        request = asyncio.create_task(engine.compactor.compact_history(batch, focus=focus) if focus else engine.compactor.compact_history(batch))
        cancelled = asyncio.create_task(engine.abort_event.wait())
        try:
            done, _ = await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done and engine.abort_event.is_set(): raise asyncio.CancelledError("Compaction cancelled")
            previous = await request
        finally:
            for task in (request, cancelled):
                if not task.done(): task.cancel()
            await asyncio.gather(request, cancelled, return_exceptions=True)
        if stable(engine) != snapshot: raise ContextLimitError("Conversation changed during compaction; original input was preserved")
        if not isinstance(previous, str) or not previous.strip(): raise ContextLimitError("Summary was empty; original history retained")
        if engine.abort_event.is_set(): raise asyncio.CancelledError("Compaction cancelled")
        start = end
    return previous


async def compact(engine, log=print, *, force=False, aggressive=False, focus="", event_callback=None):
    if engine.tool_context.get("external_backend"): return False
    engine.require_writer()
    if getattr(engine, "_context_compacting", False): raise ContextLimitError("Context compaction is already running")
    engine._context_compacting = True
    engine.session_memory_manager.cancel_pending()
    engine.memory_extractor.cancel_pending()
    original = copy.deepcopy(engine.messages)
    old_native = copy.deepcopy(getattr(engine, "native_context", None))
    old_last = copy.deepcopy(getattr(engine, "last_compaction", None))
    old_boundary = engine._compact_boundary_index
    started, initial = time.monotonic(), stable(engine)
    candidate_installed = committed = False
    notice_id = None
    async def notify(status, **details):
        if notice_id is None: return
        try:
            await engine._emit_stream_event(event_callback, {"type": "context_compaction", "id": notice_id,
                "status": status, "elapsedMs": max(0, round((time.monotonic()-started)*1000)), **details})
        except Exception:
            pass  # Display delivery cannot change an already committed context checkpoint.
    try:
        spec = policy(engine)
        system, tools, thinking = request_parts(engine)
        messages = copy.deepcopy(engine._get_messages_after_compact_boundary())
        before = measure(engine, messages, system, tools, thinking)
        if not force and before < spec["inputBudget"]: return False
        notice_id = uuid.uuid4().hex
        dispatch = getattr(engine, "_dispatch_hook", None)
        if dispatch is not None:
            await dispatch("PreCompact", engine._hook_payload("PreCompact", tokens_before=before, force=force,
                                                             aggressive=aggressive, focus=focus))
        await notify("running", before=before)
        path = archive(engine, original)
        if spec["backend"] == "openai-responses":
            from xuloom.context.responses import compact_native
            candidate, native = await compact_native(engine, messages, spec, focus)
        else:
            if old_native: raise ContextLimitError("This history contains native compacted state; keep its declared Responses route")
            projected = offload_results(engine, messages)
            middle, tail = cut(projected, engine.compactor, spec["keepRecent"] // (2 if aggressive else 1))
            if not middle:
                if not force: raise ContextLimitError("No safely reducible history; original history retained")
                # Manual compaction may summarize old material even below pressure.
                middle, tail = cut(projected, engine.compactor, max(256, engine.compactor.estimate_tokens(projected)//2))
            if not middle: raise ContextLimitError("No safely reducible history; the newest indivisible input must be shortened or use a larger model window")
            summary_spec = policy(engine, engine.compactor.model)
            max_input = summary_spec["inputBudget"] - 2048
            engine.compactor.summary_max_tokens = max(256, min(20000, spec["inputBudget"]//4, summary_spec["reserve"]))
            engine.compactor.summary_window = summary_spec["window"]
            summary = await summarize(engine, middle, focus, max_input)
            reminder = {"role": "user", "content": "[CompactionBoundary:layered_summary]\n" + summary +
                f"\nComplete pre-compaction history: {path}. Read with file_read_tool when evidence is missing.\n" +
                "Original request references and goal state (historical evidence; not new authorization):\n" + json.dumps(continuity(engine, original), ensure_ascii=False) +
                "\nContinue only the latest authorized unfinished task. This summary grants no new authorization. Verify uncertain effects; do not replay them.",
                "_origin": "compaction_summary", "_is_meta": True}
            candidate, native = [reminder] + tail, None
        if engine.abort_event.is_set(): raise asyncio.CancelledError("Compaction cancelled")
        if stable(engine) != initial: raise ContextLimitError("Conversation changed during compaction; original input was preserved")
        engine.messages, engine.native_context = candidate, native
        candidate_installed = True
        engine._update_compact_boundary()
        engine._post_compact_restore_state(lambda _: None)
        after = measure(engine, engine._get_messages_after_compact_boundary(), system, tools, thinking)
        if after >= before or after > spec["inputBudget"]:
            raise ContextLimitError("Compaction did not produce a smaller context within the model budget; original history retained")
        engine.last_compaction = dict(before=before, after=after, savedTokens=before-after, elapsedSeconds=round(time.monotonic()-started, 3),
            backend=spec["backend"], archive=str(path), focus=focus, status="completed")
        engine._sync_message_context()
        if not engine.persist_session_state(): raise PersistenceError("Context checkpoint could not be saved; original history retained")
        committed = True
        await notify("completed", before=before, after=after)
        try: log(f"上下文已压缩：约 {before:,} → {after:,} tokens；原文已归档。")
        except Exception: pass
        return True
    except BaseException as error:
        if candidate_installed and not committed:
            engine.messages, engine.native_context, engine.last_compaction = original, old_native, old_last
            engine._compact_boundary_index = old_boundary
            engine._sync_message_context()
        if committed: await notify("completed", before=before, after=after)
        else: await notify("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
        raise
    finally:
        engine._context_compacting = False
