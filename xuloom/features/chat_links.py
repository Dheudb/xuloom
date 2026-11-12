"""Chat addresses select known history/SSH targets; never commands or new paths."""
import asyncio
import json
import os
from pathlib import Path
import re

from xuloom.core.storage import data_directory, history_root, IOPath, logical_path

_ID = r"[A-Za-z0-9_-]{1,128}"
_LINK = re.compile(r"xuloom://chat/v1/([0-9a-f]{16})/(" + _ID + r")", re.ASCII)
_REMOTE_LINK = re.compile(r"xuloom://chat/ssh/v1/([0-9a-f]{64})/(" + _ID + r")", re.ASCII)


class ChatLinkError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_link(url):
    match = (_LINK.fullmatch(url) or _REMOTE_LINK.fullmatch(url)) if isinstance(url, str) and len(url) <= 512 else None
    if not match:
        raise ChatLinkError("invalid_link", "无效的 Xuloom 聊天链接；仅支持本机 v1 或 SSH v1 会话地址")
    return match.groups()


def chat_link(cwd, session_id):
    if not isinstance(session_id, str) or not re.fullmatch(_ID, session_id, re.ASCII):
        raise ChatLinkError("invalid_id", "会话 ID 无效")
    return "xuloom://chat/v1/" + history_root(cwd).name + "/" + session_id


def remote_chat_link(profile, session_id):
    from xuloom.remote.config import profile_key
    if not isinstance(session_id, str) or not re.fullmatch(_ID, session_id, re.ASCII):
        raise ChatLinkError("invalid_id", "会话 ID 无效")
    return "xuloom://chat/ssh/v1/" + profile_key(profile) + "/" + session_id


def _read(path, root):
    # History may be removed while resolving. Never follow an address to a file
    # outside central storage, including Windows directory junctions.
    try:
        path, root = IOPath(logical_path(path)), IOPath(logical_path(root))
        logical_path(path.resolve()).relative_to(logical_path(root.resolve()))
        probe = path
        while probe != root:
            if probe.is_symlink() or getattr(probe, "is_junction", lambda: False)():
                raise ValueError("linked history")
            probe = probe.parent
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        raise ChatLinkError("history_unreadable", "会话存储无法读取，请检查集中历史目录") from None


def resolve_link(url, *, require_openable=False):
    key, sid = parse_link(url)
    if len(key) == 64:
        return _resolve_remote(key, sid, require_openable=require_openable)
    root = data_directory() / "history"

    def candidate(folder, moved=False):
        raw = _read(folder / "sessions" / (sid + ".json"), root)
        if raw is None:
            return None
        manifest = _read(folder / "project.json", root)
        project = manifest.get("project") if isinstance(manifest, dict) and manifest.get("version") == 1 else None
        if not isinstance(project, str) or not Path(project).is_absolute() or history_root(project).name != folder.name:
            raise ChatLinkError("history_unreadable", "会话工作目录索引无效，请检查集中历史目录")
        metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
        if not (isinstance(raw, list) or isinstance(raw, dict) and isinstance(raw.get("messages"), list)) or not isinstance(metadata, dict):
            raise ChatLinkError("history_unreadable", "会话记录损坏，无法打开")
        if moved:
            moves = metadata.get("workspace_moves", [])
            if not isinstance(moves, list) or not moves or not isinstance(moves[-1], dict) or moves[-1].get("to") != project:
                return None
            if not any(isinstance(move, dict) and isinstance(move.get("from"), str) and
                       Path(move["from"]).is_absolute() and history_root(move["from"]).name == key for move in moves):
                return None
        index = _read(folder / "index.json", root)
        if index is not None and not (isinstance(index, dict) and index.get("version") == 1 and isinstance(index.get("sessions"), dict)):
            raise ChatLinkError("history_unreadable", "会话列表索引损坏，无法打开")
        item = (index or {}).get("sessions", {}).get(sid, {})
        if not isinstance(item, dict):
            raise ChatLinkError("history_unreadable", "会话属性无效，无法打开")
        return {"id": folder.name + ":" + sid, "session_id": sid, "project": project,
                "scope": "standalone" if item.get("scope") == "standalone" else "project",
                "archived": bool(item.get("archived")), "moved": moved}

    selected = candidate(root / key)
    if selected is None and root.is_dir():
        matches = []
        for folder in root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{16}", folder.name) or folder.name == key:
                continue
            # Only inspect histories containing this exact session ID. An
            # unrelated damaged workspace must not invalidate every link.
            if not (folder / "sessions" / (sid + ".json")).exists():
                continue
            row = candidate(folder, moved=True)
            if row:
                matches.append(row)
        if len(matches) > 1:
            raise ChatLinkError("ambiguous_link", "发现多个迁移后的会话，无法安全选择，请从历史列表打开")
        selected = matches[0] if matches else None
    if selected is None:
        raise ChatLinkError("not_found", "本机找不到此会话，可能已删除或来自另一台电脑；聊天链接不包含聊天内容")
    settings = _read(data_directory() / "workbench.json", data_directory())
    if settings is not None and not isinstance(settings, dict):
        raise ChatLinkError("history_unreadable", "工作台配置损坏，无法判断归档状态")
    project_key = os.path.normcase(os.path.abspath(selected["project"]))
    project_meta = (settings or {}).get("projectMeta", {})
    if not isinstance(project_meta, dict) or not isinstance(project_meta.get(project_key, {}), dict):
        raise ChatLinkError("history_unreadable", "工作台项目属性损坏")
    selected["archived"] |= bool(project_meta.get(project_key, {}).get("archived"))
    selected["workspace_exists"] = Path(selected["project"]).is_dir()
    if require_openable:
        if selected["archived"]:
            raise ChatLinkError("archived", "此会话或项目已归档，请先在设置的已归档会话页面恢复")
        if not selected["workspace_exists"]:
            raise ChatLinkError("workspace_missing", "会话工作目录已不存在，请恢复目录后再打开；不会自动新建空会话")
    return selected


def _resolve_remote(key, sid, *, require_openable):
    # Resolution is local and read-only. Connecting/opening the known target
    # is a separate explicit operation; a URI can never install an SSH profile.
    from xuloom.remote.config import profiles, profile_key, target, validate_profile
    settings = _read(data_directory() / "workbench.json", data_directory())
    if settings is None: settings = {}
    if not isinstance(settings, dict):
        raise ChatLinkError("history_unreadable", "工作台配置损坏，无法解析远端会话")
    chats, groups = settings.get("remoteChats", {}), settings.get("remoteProjects", {})
    if not isinstance(chats, dict) or not isinstance(groups, dict):
        raise ChatLinkError("history_unreadable", "远端会话导航配置损坏")
    identity = "ssh:" + key + ":" + sid
    saved = chats.get(identity)
    try:
        configured = profiles()
        if saved is not None:
            if not isinstance(saved, dict) or saved.get("sessionId") != sid or profile_key(saved.get("binding")) != key:
                raise ValueError("invalid reference")
            binding = validate_profile(saved["binding"])
            current = next((item for item in configured if item["id"] == binding["id"]), None)
            if current is None:
                raise ChatLinkError("connection_missing", "此会话的 SSH 连接配置已删除；请恢复配置后打开")
            if target(current) != target(binding):
                raise ChatLinkError("remote_target_changed", "SSH 连接目标已更改；旧链接不会自动改投新主机或目录")
        else:
            current = next((item for item in configured if profile_key(item) == key), None)
            if current is None:
                raise ChatLinkError("not_found", "找不到此链接对应的已配置 SSH 目标；链接不会导入主机或创建会话")
        group = groups.get("ssh:" + key, {})
        if not isinstance(group, dict): raise ValueError("invalid group")
        machine = saved.get("machine") if saved else None
        if machine is not None and (not isinstance(machine, dict) or any(not isinstance(machine.get(field), str) or not machine[field] for field in ("hostname", "platform", "cwd"))):
            raise ValueError("invalid machine identity")
    except ChatLinkError:
        raise
    except (ValueError, TypeError):
        raise ChatLinkError("history_unreadable", "SSH 连接或会话绑定无效；未尝试连接远端") from None
    archived = bool((saved or {}).get("archived") or group.get("archived"))
    if require_openable and archived:
        raise ChatLinkError("archived", "此远端会话或工作区已归档，请先在设置中恢复")
    return {"transport": "ssh", "id": identity, "session_id": sid, "connection_id": current["id"],
            "binding": current, "machine": machine, "archived": archived, "remote_verified": False}


async def read_link(url, *, cursor=None, limit=40, agent_id=None):
    """Read a saved transcript without opening, initializing or resuming its engine."""
    resolved = await asyncio.to_thread(resolve_link, url)
    if resolved.get("transport") == "ssh":
        from xuloom.remote.proxy import SSHBridge
        bridge = SSHBridge(resolved["connection_id"], resolved["binding"], resolved["machine"], lambda _: None)
        try:
            page = await bridge.dispatch("session.transcript", {"sessionId": resolved["session_id"],
                "cursor": cursor, "limit": limit, "agentId": agent_id})
        finally:
            await bridge.close()
        return {**page, "url": url, "transport": "ssh", "archived": resolved["archived"]}
    from xuloom.features.agent_history import inspect
    page = await inspect(resolved["project"], resolved["session_id"], cursor=cursor, limit=limit, agent_id=agent_id)
    return {**page, "url": url, "transport": "local", "project": resolved["project"],
            "archived": resolved["archived"], "moved": resolved["moved"],
            "workspaceExists": resolved["workspace_exists"]}
