"""Create user-authored definitions using the existing capability registries."""
import os
import re
import yaml
from xuloom.agents.builtin_agents import BUILTIN_AGENTS
from xuloom.agents.registry import tool_list


def create_definition(registry, params):
    kind, scope = params.get("kind"), params.get("scope")
    if kind not in {"skills", "agents"} or scope not in {"project", "global"}:
        raise ValueError("请选择能力类型和保存范围")
    name, description, prompt = (str(params.get(key, "")).strip() for key in ("name", "description", "prompt"))
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("名称须为小写字母、数字和单个连字符，最多 64 个字符")
    if not description or len(description) > 1024 or not prompt or len(prompt) > 100000:
        raise ValueError("请填写描述（最多 1024 字符）与指令（最多 100000 字符）")
    metadata = {"name": name, "description": description}
    if kind == "agents":
        if name in BUILTIN_AGENTS:
            raise ValueError("不能覆盖内置代理角色")
        model = str(params.get("model") or "inherit").strip()
        if len(model) > 256: raise ValueError("模型 ID 过长")
        metadata["model"] = model
        from xuloom.agents.executors import executor, profile
        location = executor(params.get("executor"))
        backend = params.get("backend", "native")
        if backend not in {"native", "claude"}:
            raise ValueError("请选择原生引擎或已安装的 Claude Code")
        program = params.get("backend_program")
        if backend != "native":
            metadata["backend"] = backend
            if program:
                if not isinstance(program, str) or len(program) > 4096:
                    raise ValueError("外部程序路径无效")
                metadata["backend_program"] = program
            if location == "local":
                from xuloom.agents.external.program import resolve
                resolve(program)
        if location != "local":
            profile(location)
            metadata["executor"] = location
        connection = params.get("connection")
        if connection:
            if not isinstance(connection, str) or not connection.strip() or len(connection) > 256:
                raise ValueError("模型连接 ID 无效")
            if location == "local":
                from xuloom.agents.connections import resolve_connection
                binding, _ = resolve_connection(connection, None if model == "inherit" else model)
                if backend == "claude" and binding["provider"] != "anthropic":
                    raise ValueError("Claude Code 后端需要 Anthropic Messages 协议的连接")
            metadata["connection"] = connection
        if params.get("tools", "").strip():
            tools = tool_list(params["tools"])
            if any("(" in tool for tool in tools): raise ValueError("请使用工具名称，不支持命令模式")
            metadata["tools"] = tools
        skills = tool_list(params.get("skills", ""), aliases=False)
        if location == "local":
            for skill in skills: registry.resolve(skill)
        if skills: metadata["skills"] = skills
    base = registry.home if scope == "global" else registry.cwd
    root = base / ".xuloom" / kind
    target = root / name / "SKILL.md" if kind == "skills" else root / (name + ".md")
    probe = target
    while probe != base:
        if probe.is_symlink() or (probe.exists() and getattr(probe.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("不写入链接目录或链接文件")
        probe = probe.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n\n" + prompt + "\n"
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise ValueError("同名文件已存在，不会覆盖；请修改名称或打开配置目录编辑") from None
    return str(target)
