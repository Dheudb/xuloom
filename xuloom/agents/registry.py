"""Load custom subagent definitions without conflating them with skills."""
import yaml
from xuloom.agents.builtin_agents import BUILTIN_AGENTS
from xuloom.skills.registry import SkillRegistry, read_frontmatter

TOOL_ALIASES = {"Read": "file_read_tool", "Write": "file_write_tool", "Edit": "file_edit_tool", "Bash": "bash_tool", "Grep": "grep_tool", "Glob": "glob_tool", "Skill": "skill_tool", "Exec": "command_tool"}


def tool_list(value, aliases=True):
    if value is None:
        return ["*"]
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("tools and skills must be lists or comma-separated strings")
    return [TOOL_ALIASES.get(v, v) if aliases else v for v in value]


class AgentRegistry:
    def __init__(self, cwd=None, home=None):
        self.skills = SkillRegistry(cwd, home)

    def discover(self):
        entries, names = [], set()
        for root, scope in self.skills.roots("agents"):
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.md")):
                entry = {"name": path.stem, "path": str(path.resolve()), "scope": scope, "valid": True, "error": "", "shadowed": False}
                try:
                    metadata, body = read_frontmatter(path)
                    name, description = metadata.get("name"), metadata.get("description")
                    if not isinstance(name, str) or not name.strip() or not isinstance(description, str) or not description.strip() or not body:
                        raise ValueError("name, description and a prompt body are required")
                    if metadata.get("model") is not None and not isinstance(metadata["model"], str):
                        raise ValueError("model must be a string")
                    if metadata.get("model") == "inherit":
                        metadata["model"] = None
                    connection = metadata.get("connection")
                    if connection is not None and (not isinstance(connection, str) or not connection.strip() or len(connection) > 256):
                        raise ValueError("connection must be a non-empty model connection ID")
                    effort = metadata.get("reasoning_effort")
                    from xuloom.core.reasoning import EFFORTS
                    if effort is not None and effort not in EFFORTS:
                        raise ValueError("Unsupported reasoning_effort")
                    web = metadata.get("web_search", "inherit")
                    if web not in {"inherit", "disabled", "enabled"}:
                        raise ValueError("web_search must be inherit, disabled or enabled")
                    servers = metadata.get("mcp_servers")
                    if servers is not None and (not isinstance(servers, list) or not all(isinstance(v, str) for v in servers)):
                        raise ValueError("mcp_servers must be a list of server names; [] disables MCP")
                    readonly = metadata.get("read_only", False)
                    from xuloom.agents.executors import executor
                    location = executor(metadata.get("executor"))
                    backend = metadata.get("backend", "native")
                    program = metadata.get("backend_program")
                    if backend not in {"native", "claude"}:
                        raise ValueError("backend must be native or claude (an installed Claude Code CLI)")
                    if program is not None and (backend == "native" or not isinstance(program, str) or not program.strip()):
                        raise ValueError("backend_program requires an external backend and an absolute installed path")
                    if type(readonly) is not bool:
                        raise ValueError("read_only must be a boolean")
                    if "sandbox_mode" in metadata:
                        raise ValueError("sandbox_mode is not supported; use explicit tools and read_only (not an OS sandbox)")
                    entry.update(name=name, description=description, system_prompt=body, model=metadata.get("model"), allowed_tools=tool_list(metadata.get("tools")), skills=tool_list(metadata.get("skills", []), aliases=False), agent_type=name, agent_role="custom", disallowed_tools=set())
                    entry.update(reasoning_effort=effort, web_search=web, mcp_servers=servers, read_only=readonly, connection=connection, executor=location)
                    entry.update(backend=backend, backend_program=program)
                    if name in BUILTIN_AGENTS:
                        raise ValueError("Custom agent name conflicts with a built-in agent")
                    if any("(" in tool for tool in entry["allowed_tools"]):
                        raise ValueError("Per-command tool patterns are unsupported; use exact tool names")
                    if location == "local":
                        for skill_name in entry["skills"]:
                            self.skills.resolve(skill_name)
                    entry["shadowed"] = name in names
                    names.add(name)
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    entry.update(valid=False, error=str(exc))
                entries.append(entry)
        return entries

    def definitions(self):
        result = {name: dict(value) for name, value in BUILTIN_AGENTS.items()}
        result.update({e["name"]: e for e in self.discover() if e["valid"] and not e["shadowed"]})
        return result

    def resolve(self, name):
        definitions = self.definitions()
        if name not in definitions:
            raise ValueError("Unknown or invalid subagent: " + str(name))
        return definitions[name]
