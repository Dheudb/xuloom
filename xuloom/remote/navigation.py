"""Inspect/forget local SSH navigation only; never connect or delete remote history."""
import copy
import hashlib
import json
import re

from xuloom.desktop.settings import preference_version, read_workbench, save_workbench
from xuloom.remote.config import profile_key, validate_profile
from xuloom.core.session_lock import SessionConflictError


def _selector(field, key):
    return hashlib.sha256(json.dumps([field, key], ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def _entries(settings):
    for field in ("remoteChats", "remoteProjects"):
        if field not in settings: continue
        value = settings[field]
        if not isinstance(value, dict):
            yield field, None, value
        else:
            for key, row in value.items(): yield field, key, row


def _public(field, key, value):
    item = {"id": _selector(field, key), "version": preference_version(value), "field": field,
            "kind": "container" if key is None else "entry", "valid": False,
            "title": "远端会话导航格式损坏" if field == "remoteChats" else "远端工作区显示设置损坏",
            "message": "仅保留原始本机数据；未尝试连接远端"}
    if field == "remoteChats" and key is not None and re.fullmatch(r"ssh:[0-9a-f]{64}:[A-Za-z0-9_-]{1,128}", key): item["chatId"] = key
    if key is None: return item
    try:
        if not isinstance(value, dict): raise ValueError("not an object")
        if field == "remoteChats":
            profile = validate_profile(value.get("binding"))
            sid = value.get("sessionId")
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) or key != "ssh:" + profile_key(profile) + ":" + sid: raise ValueError("invalid identity")
            if not isinstance(value.get("project"), str) or not value["project"]: raise ValueError("invalid directory")
            for name in ("created", "updated"):
                if name in value and (type(value[name]) not in (int, float) or not -8640000000000000 <= value[name] <= 8640000000000000): raise ValueError("invalid date")
            if "draftAttachments" in value and value["draftAttachments"] != []: raise ValueError("invalid attachments")
            for name, limit in (("title", 200), ("draft", 1_000_000), ("draftEditId", 128)):
                if name in value and (not isinstance(value[name], str) or len(value[name]) > limit): raise ValueError("invalid field")
                if name in value: value[name].encode("utf-8")
            if value.get("permissionMode", "ask") not in {"ask", "full"}: raise ValueError("invalid permission")
            machine = value.get("machine")
            if machine is not None and (not isinstance(machine, dict) or any(not isinstance(machine.get(name), str) or not machine[name] for name in ("hostname", "platform", "cwd"))): raise ValueError("invalid machine")
            item.update(title=value.get("title") or "新会话", host=profile["host"], connection=profile["name"])
        elif not re.fullmatch(r"ssh:[0-9a-f]{64}", key): raise ValueError("invalid group")
        elif "name" in value and (not isinstance(value["name"], str) or not value["name"].strip() or len(value["name"]) > 100): raise ValueError("invalid name")
        if field == "remoteProjects" and "name" in value: value["name"].encode("utf-8")
        for name in ("open", "collapsed", "pinned", "archived"):
            if name in value and type(value[name]) is not bool: raise ValueError("invalid flag")
        item.update(valid=True, message="仅为本机导航；远端连接及历史状态未查询")
        if field == "remoteProjects": item["title"] = value.get("name") or "远端工作区显示设置"
    except (ValueError, TypeError): pass
    return item


def references():
    return [_public(*entry) for entry in _entries(read_workbench())]


def forget(selector, version, *, confirm=False):
    if confirm is not True: raise ValueError("请确认移除本机引用及已保存的本机草稿；远端历史不会删除")
    if not isinstance(selector, str) or not re.fullmatch(r"[0-9a-f]{64}", selector) or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version): raise ValueError("Invalid navigation selection")
    before = read_workbench()
    selected = next((entry for entry in _entries(before) if _selector(entry[0], entry[1]) == selector), None)
    if selected is None or preference_version(selected[2]) != version:
        raise SessionConflictError("导航引用已更改，请刷新后重试；未移除新数据")
    field, key, _ = selected
    after = copy.deepcopy(before)
    if key is None: after[field] = {}
    else: del after[field][key]
    result = save_workbench({"before": before, "after": after, "navigation_removal": {"field": field, "key": key, "version": version}})
    return {"removed": True, "backup": result.get("backup")}
