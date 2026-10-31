"""Local chat addresses. Links select saved history; they never supply commands or paths."""
import json
import os
from pathlib import Path
import re

from xuloom.core.storage import data_directory, history_root

_ID = r"[A-Za-z0-9_-]{1,128}"
_LINK = re.compile(r"xuloom://chat/v1/([0-9a-f]{16})/(" + _ID + r")", re.ASCII)


class ChatLinkError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_link(url):
    match = _LINK.fullmatch(url) if isinstance(url, str) and len(url) <= 512 else None
    if not match:
        raise ChatLinkError("invalid_link", "无效的 Xuloom 聊天链接；仅支持本地 v1 会话地址")
    return match.groups()


def chat_link(cwd, session_id):
    if not isinstance(session_id, str) or not re.fullmatch(_ID, session_id, re.ASCII):
        raise ChatLinkError("invalid_id", "会话 ID 无效")
    return "xuloom://chat/v1/" + history_root(cwd).name + "/" + session_id


def _read(path, root):
    # History may be removed while resolving. Never follow an address to a file
    # outside central storage, including Windows directory junctions.
    try:
        path.resolve().relative_to(root.resolve())
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
