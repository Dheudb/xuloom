import re
from pydantic import BaseModel, Field

from xuloom.context.tool_results import build_tool_result
from xuloom.tools.base import BaseAgenticTool


class ToolSearchToolInput(BaseModel):
    query: str = Field(
        ...,
        description="Describe the capability you need, such as browser automation, web search, notebook editing, or language-server diagnostics.",
    )
    activate: list[str] = Field(
        default_factory=list,
        description="Optional list of matching latent tool names to activate for the current session.",
    )


class ToolSearchTool(BaseAgenticTool):
    name = "tool_search_tool"
    description = "Searches and activates specialized tools, including guarded tool sequences for already-decided dependent actions."
    input_schema = ToolSearchToolInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        return (
            "If you need a specialized capability that is not currently exposed as an active tool, "
            "use `tool_search_tool` to discover and activate matching tools first, including connected MCP tools."
            " Discover web search and URL reading here when not already loaded. "
            "This tool searches tool definitions, not the internet."
        )

    async def execute(self, query: str, activate: list[str] = None) -> dict:
        activate = activate or []
        query_lower = (query or "").strip().lower()
        active_tools = self.context.get("engine_available_tools", {}) or {}
        latent_tools = self.context.get("latent_tools_registry", {}) or {}
        activate_tools = self.context.get("activate_tools")
        current_names = self.context.get("model_tool_names")
        model_names = current_names() if callable(current_names) else set(active_tools)
        isolation = self.context.get("execution_sandbox")
        if isolation and isolation.enabled:
            from xuloom.security.execution import FILE_TOOLS, CONTROL_TOOLS, PROCESS_TOOLS
            permitted = FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS
            active_tools = {name: tool for name, tool in active_tools.items() if name in permitted}
            latent_tools = {name: tool for name, tool in latent_tools.items() if name in permitted}
            activate = [name for name in activate if name in permitted]

        catalog = {}
        for name, tool in active_tools.items():
            catalog[name] = {
                "name": name,
                "description": getattr(tool, "description", ""),
                "prompt": tool.prompt() if callable(getattr(tool, "prompt", None)) else "",
                "active": name in model_names,
            }
        for name, tool in latent_tools.items():
            catalog[name] = {
                "name": name,
                "description": getattr(tool, "description", ""),
                "prompt": tool.prompt() if callable(getattr(tool, "prompt", None)) else "",
                "active": False,
            }

        matches = []
        terms = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", query_lower))
        terms -= {"the", "a", "an", "for", "to", "of", "and", "or", "with", "use", "tool", "tools", "access", "information", "request", "please"}
        aliases = {"续跑": {"sequence"}, "工具序列": {"sequence"}, "条件执行": {"sequence"}, "联网": {"web", "search"}, "搜索网页": {"web", "search"}, "网页": {"web", "fetch"},
                   "浏览器": {"browser"}, "笔记本": {"notebook"}, "internet": {"web"}, "url": {"fetch"}, "browse": {"browser"},
                   "图片": {"image", "generate"}, "视频": {"video", "generate"}, "音乐": {"music"}, "语音": {"speech"}, "音效": {"sound"}}
        for alias, expanded in aliases.items():
            if alias in query_lower:
                terms.update(expanded)
        for item in catalog.values():
            haystack = " ".join([
                item.get("name", ""),
                item.get("description", ""),
                item.get("prompt", ""),
            ]).lower()
            name = item["name"].lower()
            description = item["description"].lower()
            score = sum(5 if term in name else 3 if term in description else 1 if term in haystack else 0 for term in terms)
            if not query_lower or query_lower in haystack or score:
                item["score"] = score + (20 if query_lower and query_lower in haystack else 0)
                matches.append(item)

        matches = sorted(matches, key=lambda item: (-item["score"], item.get("active") is False, item.get("name")))

        activated = []
        if activate and callable(activate_tools):
            activated = activate_tools(activate)
        for item in matches:
            if item['name'] in activated:
                item['active'] = True

        lines = [f"Matched tools for query '{query}':"]
        if matches:
            for item in matches[:12]:
                state = "active" if item.get("active") else "latent"
                lines.append(f"- {item['name']} [{state}] {item['description']}")
        else:
            lines.append("- No matching tools found.")

        if activated:
            lines.append("")
            lines.append("Activated tools:")
            for name in activated:
                lines.append(f"- {name}")
        # Return the existing contract on explicit activation or exact lookup.
        # Do not permanently inject every tool manual into every model request.
        for name in dict.fromkeys(activated + ([query] if query in catalog else [])):
            if catalog[name]["prompt"]:
                lines.append(f"\nUsage contract for {name}:\n{catalog[name]['prompt']}")
        if any(not item["active"] for item in matches):
            lines.append("To activate a latent tool, call tool_search_tool again with activate=[the exact tool name].")

        return build_tool_result(
            ok=True,
            content="\n".join(lines),
            metadata={
                "query": query,
                "match_count": len(matches),
                "activated": activated,
                "active_tool_count": len(active_tools),
                "latent_tool_count": len(latent_tools),
            },
        )
