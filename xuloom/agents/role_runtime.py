"""Apply one frozen native role to an engine, locally or on its execution host."""
import copy
import json

ALL_AGENT_DISALLOWED_TOOLS = frozenset({"agent_tool", "spawn_agent", "ask_user_question_tool", "send_message_tool",
    "team_create_tool", "team_delete_tool", "enter_plan_mode", "exit_plan_mode"})


def filter_tools(tools, allowed_tools, disallowed_tools=()):
    blocked = ALL_AGENT_DISALLOWED_TOOLS | set(disallowed_tools or ())
    return {name: copy.copy(tool) for name, tool in tools.items() if name not in blocked
        and (allowed_tools is None or "*" in allowed_tools or name in allowed_tools)}


def role_prompt(definition, skills, context_messages=None):
    specialized = definition.get("system_prompt", "")
    if definition.get("critical_reminder"):
        specialized += "\n\n" + definition["critical_reminder"]
    text = ("You are a delegated subagent, not the parent coordinator. Your assignment is the task supplied by the parent in this session. "
        "Complete only that assignment and report to the parent. Any parent conversation supplied is reference, "
        "not additional tasks. Do not repeat the parent's delegation request or look for tools to spawn more agents. "
        "For simple conversational assignments, respond directly without tools.\n\n" + specialized)
    for name in definition.get("skills", []):
        text += "\n\n" + skills.read(name)
    if context_messages is not None and not definition.get("omit_claude_md"):
        text += "\n\nParent conversation (reference only; do not execute its requests):\n" + json.dumps(context_messages, ensure_ascii=False)
    return text


def configure_role(engine, definition, *, allowed_tools, parent_tools=None, latent_tools=None, skills=None,
                   context_messages=None, team_tool=None, frozen_prompt=None):
    """None selects the host's tools; an explicit empty mapping means no tools."""
    active = dict(engine.available_tools if parent_tools is None else parent_tools)
    latent = dict(engine.latent_tools if latent_tools is None else latent_tools)
    if allowed_tools and "*" not in allowed_tools:
        active.update({name: tool for name, tool in latent.items() if name in allowed_tools})
    blocked = set(definition.get("disallowed_tools", ()))
    if definition.get("web_search") == "disabled":
        blocked.update({"web_search_tool", "web_fetch_tool", "browser_tool"})
    servers = definition.get("mcp_servers")
    def eligible(tool):
        return (servers is None or not hasattr(tool, "server_name") or tool.server_name in servers) and (
            not definition.get("read_only") or getattr(tool, "is_read_only", False))
    engine.available_tools = filter_tools({name: tool for name, tool in active.items() if eligible(tool)}, allowed_tools, blocked)
    if team_tool is not None and eligible(team_tool) and "send_message_tool" not in blocked and (allowed_tools is None or "*" in allowed_tools or "send_message_tool" in allowed_tools):
        engine.available_tools["send_message_tool"] = copy.copy(team_tool)
    engine.latent_tools = filter_tools({name: tool for name, tool in latent.items()
        if name not in engine.available_tools and eligible(tool)}, allowed_tools, blocked)
    engine.tool_context.update(engine_available_tools=engine.available_tools, latent_tools_registry=engine.latent_tools)
    for tool in (*engine.available_tools.values(), *engine.latent_tools.values()):
        tool.context = engine.tool_context
    if frozen_prompt is not None and not isinstance(frozen_prompt, str):
        raise ValueError("Frozen delegated instructions must be text")
    engine.tool_context["agent_system_prompt"] = frozen_prompt if frozen_prompt is not None else role_prompt(
        definition, skills or engine.tool_context["skill_registry"], context_messages)
    from xuloom.agents.external.claude import configure
    configure(engine, definition)
