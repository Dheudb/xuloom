"""Merge desktop preference edits while holding the shared OS writer lease."""
import json
import hashlib
import time
import uuid

from xuloom.core.session_lock import SessionLock, SessionBusyError, SessionConflictError
from xuloom.core.storage import data_directory, atomic_json, atomic_text


def preference_version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def read_workbench():
    path = data_directory() / "workbench.json"
    if path.is_symlink() or path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise SessionConflictError("Desktop settings cannot be a link or junction")
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict): raise ValueError("Invalid desktop settings; original file was preserved")
    return value


def _merge(before, after, current, field=""):
    if before == after:
        return current
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(current, dict) and field != "activeConversation":
        merged = dict(current)
        for key in before.keys() | after.keys():
            if key not in after:
                if key in current and key in before and current[key] != before[key]:
                    raise SessionConflictError("要移除的设置已由另一实例更改，请刷新后重试；原数据已保留。")
                merged.pop(key, None)
            elif key not in before or before[key] != after[key]:
                if key in before and key not in current:
                    raise SessionConflictError("桌面设置已由另一实例移除，请刷新后重试；未覆盖当前设置。")
                empty = [] if isinstance(after[key], list) else {}
                merged[key] = _merge(before.get(key, empty), after[key], current.get(key, empty), key)
        return merged
    if field in {"projects", "standaloneDirectories"} and all(isinstance(value, list) for value in (before, after, current)):
        removed = set(before) - set(after)
        retained = [item for item in current if item not in removed]
        added = [item for item in after if item not in before]
        reordered = [item for item in after if item in before] != [item for item in before if item in after]
        prefix = [item for item in after if item in retained or item in added] if reordered else added
        return list(dict.fromkeys(prefix + retained))
    return after


def save_workbench(spec):
    before, after = spec.get("before"), spec.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("Desktop settings require an original snapshot and the edited snapshot")
    changed = {key for key in before.keys() | after.keys() if key not in before or key not in after or before[key] != after[key]}
    if changed - {"projects", "projectMeta", "standaloneDirectories", "sidebar", "layout", "defaultDirectory", "workspace", "activeConversation", "remoteChats", "remoteProjects"}:
        raise ValueError("Unsupported desktop preference fields")
    lock = SessionLock(data_directory() / "session-locks", "preferences_workbench")
    for attempt in range(51):
        try:
            lock.acquire()
            break
        except SessionBusyError:
            if attempt == 50:
                raise SessionBusyError("桌面设置正被另一实例保存，请稍后重试；原设置已保留。") from None
            time.sleep(0.01)
    try:
        path = data_directory() / "workbench.json"
        if path.is_symlink() or path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise SessionConflictError("Desktop settings cannot be a link or junction")
        if not path.exists() and spec.get("legacy"):
            from pathlib import Path
            legacy = Path(spec["legacy"])
            original = legacy.read_bytes()
            value = json.loads(original.decode("utf-8-sig"))
            if not isinstance(value, dict): raise ValueError("Invalid legacy desktop settings")
            atomic_json(path, value)
            if legacy.read_bytes() == original: legacy.unlink()
        current = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(current, dict): raise ValueError("Invalid desktop settings; original file was preserved")
        removal = spec.get("navigation_removal")
        if removal is not None:
            if not isinstance(removal, dict): raise ValueError("Invalid remote navigation removal")
            field, key = removal.get("field"), removal.get("key")
            if field not in {"remoteChats", "remoteProjects"} or key is not None and not isinstance(key, str):
                raise ValueError("Invalid remote navigation removal")
            if field not in current: raise SessionConflictError("导航引用已更改，请刷新后重试")
            value = current[field]
            if key is not None:
                if not isinstance(value, dict) or key not in value: raise SessionConflictError("导航引用已更改，请刷新后重试")
                value = value[key]
            if preference_version(value) != removal.get("version"):
                raise SessionConflictError("导航引用已更改，请刷新后重试；未移除新数据")
        merged = _merge(before, after, current)
        # These two fields describe the last saving window, never a mixed snapshot.
        if changed & {"workspace", "activeConversation"}:
            for key in ("workspace", "activeConversation"):
                if key in after: merged[key] = after[key]
                elif key in before: merged.pop(key, None)
        if merged != current:
            backup = None
            if removal is not None:
                backup_root = data_directory() / "navigation-backups"
                if backup_root.is_symlink() or backup_root.exists() and getattr(backup_root.lstat(), "st_file_attributes", 0) & 0x400:
                    raise SessionConflictError("导航备份目录不能是链接；原引用已保留")
                backup = backup_root / (uuid.uuid4().hex + ".json")
                atomic_text(backup, json.dumps({"version": 1, "field": field, "key": key, "value": value, "saved_at": time.time()}, ensure_ascii=True, indent=2), create_only=True)
                if field == "remoteChats":
                    removed = lambda identity: isinstance(identity, str) and (identity == key if key is not None else identity.startswith("ssh:"))
                    selection = current.get("activeConversation")
                    if isinstance(selection, dict) and removed(selection.get("remoteId")): merged["activeConversation"] = None
                    if isinstance(current.get("workspace"), list):
                        merged["workspace"] = [row for row in current["workspace"] if not isinstance(row, dict) or not removed(row.get("remoteId"))]
            atomic_json(path, merged, ensure_ascii=True)
        return {"completed": True, "settings": merged, **({"backup": str(backup)} if merged != current and backup else {})}
    finally:
        lock.close()
