"""Small model-facing projections; execution keeps the full native contracts."""
import copy

CORE_TOOLS = frozenset({"command_tool", "file_read_tool", "file_edit_tool", "file_write_tool", "tool_search_tool"})
BRIEFS = {
    "command_tool": "Start/poll workspace commands. Prefer argv/stdin; page stdout/stderr to eof and require completed + exit_code=0. Never parse partial JSON.",
    "file_read_tool": "Read files/images. Text defaults to 2000 lines; follow next offset. force re-reads; pdf_mode=image reads figures/scans. Discover by exact tool name for usage.",
    "file_edit_tool": "Replace unique text in an already-read file.",
    "file_write_tool": "Create or overwrite a file; preserve existing work.",
    "tool_search_tool": "Find and activate tools for web search, skills, agents, plans, media or other tasks.",
}


def visible(engine):
    effective = engine._get_effective_tools()
    from xuloom.agents.ultra_execution import required, NAMES
    if required(engine):
        return {"ultra_prepare": engine.available_tools["ultra_prepare"]}
    builtin = getattr(engine, "builtin_tool_names", set())
    selected = CORE_TOOLS | engine.activated_tools | engine.tool_context.get('role_declared_tools', frozenset())
    if getattr(engine, 'collaboration_mode', '') == 'ultra':
        selected = selected | {'spawn_agent', 'web_search_tool', 'web_fetch_tool'}
    if getattr(engine, 'coordinator_mode', False):
        selected = selected | {'spawn_agent', 'agent_tool', 'plan_tool', 'todo_write_tool'}
    if engine.tool_context.get('team_member_name'):
        selected = selected | {'send_message_tool'}
    if getattr(engine, 'plan_manager', None) and engine.plan_manager.get_mode() == 'plan':
        selected = selected | {'exit_plan_mode'}
    return {name: tool for name, tool in effective.items() if name not in NAMES and (name in selected or name not in builtin)}


def compact_definition(definition):
    """Keep JSON types, references, required fields and all validation bounds."""
    def project(value, names=False):
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if not names and key in {"title", "examples"}:
                continue
            if not names and key == "description" and isinstance(item, str):
                text = " ".join(item.split())
                result[key] = text if len(text) <= 100 else text[:97].rstrip() + "..."
            elif names or key in {'properties', '$defs', 'definitions', 'patternProperties', 'items',
                    'additionalProperties', 'unevaluatedProperties', 'contains', 'propertyNames',
                    'not', 'if', 'then', 'else', 'allOf', 'anyOf', 'oneOf', 'prefixItems'}:
                result[key] = project(item, key in {'properties', '$defs', 'definitions', 'patternProperties'})
            else:
                result[key] = copy.deepcopy(item)
        return result
    result = copy.deepcopy(definition)
    result["input_schema"] = project(result.get("input_schema", {}))
    result["description"] = BRIEFS.get(result.get("name"), " ".join(result.get("description", "").split())[:160])
    if result.get("name") in CORE_TOOLS:
        hints = {"argv": "Literal executable/arguments; no shell interpolation.",
            "wait_ms": "Wait milliseconds; may return earlier.", "stdin": "Exact UTF-8 input.",
            "pages": "PDF page range, e.g. 1-5.", "limit": "Number of text lines.",
            "offset": "Byte cursor." if result["name"] == "command_tool" else "1-based line; negative counts from end.",
            "char_offset": "0-based char cursor; excludes offset/limit.",
            "force": "Re-deliver already-read evidence, including after compaction.",
            "pdf_mode": "auto: native PDF or text; text: extraction; image: selected page images (max 10).",
            "activate": "Tool names to activate, optionally with search."}
        for field, spec in result["input_schema"].get("properties", {}).items():
            if field in hints:
                spec["description"] = hints[field]
            else:
                spec.pop("description", None)
    return result
