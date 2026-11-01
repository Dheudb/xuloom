"""Small source/artifact projections over saved execution receipts; no model or task runner."""
import os
import re
from urllib.parse import urlsplit

IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
WRITERS = {"file_write_tool", "file_edit_tool", "notebook_edit_tool"}


def rows(value):
    return value if isinstance(value, list) else []


def human_turn(messages):
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else ()
        if (message.get("role") == "user" and message.get("_uuid")
                and message.get("_origin", "human") == "human"
                and not message.get("_is_meta") and not message.get("_is_tool_result")
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)):
            return message["_uuid"]
    return "host"


def source_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        parsed.port
        return value
    except ValueError:
        return None


def merge_references(destination, incoming, *, agent_id=None):
    if not isinstance(incoming, dict):
        return
    for turn, agents in incoming.items():
        if not isinstance(turn, str) or not IDENTITY.fullmatch(turn) or not isinstance(agents, dict):
            continue
        for identity, contribution in agents.items():
            if agent_id is not None and identity != agent_id:
                continue
            if not isinstance(identity, str) or not IDENTITY.fullmatch(identity) or not isinstance(contribution, dict):
                continue
            count = contribution.get("count")
            if type(count) is not int or count < 0:
                continue
            group = destination.setdefault(turn, {})
            if count > group.get(identity, {}).get("count", -1):
                clean = {"count": count, "tools": {}, "files": {}, "sources": {}}
                for name, row in contribution.get("tools", {}).items() if isinstance(contribution.get("tools"), dict) else []:
                    if isinstance(name, str) and isinstance(row, dict) and all(type(row.get(k)) is int and row[k] >= 0 for k in ("count", "errors")):
                        clean["tools"][name] = {k: row[k] for k in ("count", "errors")}
                for row in contribution.get("files", {}).values() if isinstance(contribution.get("files"), dict) else []:
                    if isinstance(row, dict) and isinstance(row.get("path"), str) and "\0" not in row["path"]:
                        clean["files"][os.path.normcase(row["path"])] = {key: row[key] for key in ("path", "name", "operation", "tool", "toolUseId", "agentId") if isinstance(row.get(key), str)}
                for row in contribution.get("sources", {}).values() if isinstance(contribution.get("sources"), dict) else []:
                    if isinstance(row, dict) and source_url(row.get("url")):
                        clean["sources"][row["url"]] = {key: row[key] for key in ("url", "title", "kind", "tool", "toolUseId", "agentId") if isinstance(row.get(key), str)}
                group[identity] = clean


def record_receipt(references, *, turn_id, agent_id, tool_name, tool_use_id, result, status):
    succeeded = status in {"ok", "completed"}
    turn_id = turn_id or "host"
    group = references.setdefault(turn_id, {}).setdefault(agent_id, {"count": 0, "tools": {}, "files": {}, "sources": {}})
    group["count"] += 1
    tool = group["tools"].setdefault(tool_name, {"count": 0, "errors": 0})
    tool["count"] += 1
    tool["errors"] += not succeeded
    if not succeeded or not isinstance(result, dict) or result.get("ok") is not True or result.get("is_error"):
        return
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return
    provenance = {"tool": tool_name, "toolUseId": tool_use_id, "agentId": agent_id}
    if tool_name in WRITERS and metadata.get("write_applied") is True and isinstance(metadata.get("path"), str):
        path = metadata["path"]
        group["files"][os.path.normcase(path)] = {"path": path, "name": os.path.basename(path),
            "operation": metadata.get("operation", "write"), **provenance}
    for item in metadata.get("sources", []) if isinstance(metadata.get("sources"), list) else []:
        if not isinstance(item, dict) or not source_url(item.get("url")):
            continue
        url = item["url"]
        kind = item.get("kind") if item.get("kind") in {"search", "page", "resource"} else "resource"
        previous = group["sources"].get(url)
        if previous and previous["kind"] == "page" and kind != "page":
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        title = " ".join(title.split())[:300] or urlsplit(url).hostname
        group["sources"][url] = {"url": url, "title": title, "kind": kind, **provenance}


def project_summary(raw, cwd, *, turn_id=None, all_turns=False):
    if turn_id is not None and (not isinstance(turn_id, str) or not IDENTITY.fullmatch(turn_id)):
        raise ValueError("Invalid turn ID")
    if all_turns and turn_id:
        raise ValueError("Choose a turn or the whole conversation")
    messages = rows(raw if isinstance(raw, list) else raw.get("messages"))
    metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    references = {}
    artifacts = metadata.get("artifacts", {})
    merge_references(references, artifacts.get("references", {}) if isinstance(artifacts, dict) else {})
    selected = turn_id or human_turn(messages)
    files, sources, tools = {}, {}, {}
    ids = list(references) if all_turns else [selected]
    for identity in ids:
        for contribution in references.get(identity, {}).values():
            if not isinstance(contribution, dict):
                continue
            for name, value in contribution.get("tools", {}).items():
                row = tools.setdefault(name, {"name": name, "count": 0, "errors": 0})
                row["count"] += value.get("count", 0)
                row["errors"] += value.get("errors", 0)
            for item in contribution.get("files", {}).values():
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    files[os.path.normcase(item["path"])] = item
            for item in contribution.get("sources", {}).values():
                if isinstance(item, dict) and source_url(item.get("url")):
                    previous = sources.get(item["url"])
                    if not previous or previous.get("kind") != "page" or item.get("kind") == "page":
                        sources[item["url"]] = item
    # Existing display traces already know files and actual tool statuses. Do
    # not guess missing old sources from assistant prose or a webpage's body.
    legacy = False
    covered = set(references)
    traces = metadata.get("turn_traces", [])
    for trace in traces if isinstance(traces, list) else []:
        if not isinstance(trace, dict):
            continue
        identity = trace.get("userMessageId")
        if not isinstance(identity, (str, type(None))):
            continue
        if identity in references or not all_turns and identity != selected:
            continue
        steps = rows(trace.get("steps"))
        legacy = legacy or any(isinstance(step, dict) and isinstance(step.get("tool"), dict) for step in steps)
        covered.add(identity)
        for item in rows(trace.get("files")):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                files[os.path.normcase(item["path"])] = {"path": item["path"], "name": os.path.basename(item["path"]), "operation": item.get("operation", "write")}
        for step in steps:
            tool = step.get("tool") if isinstance(step, dict) else None
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                row = tools.setdefault(tool["name"], {"name": tool["name"], "count": 0, "errors": 0})
                row["count"] += 1
                row["errors"] += tool.get("status") != "completed"
    # Older CLI conversations have tool receipts but no desktop display trace.
    # Count the real calls and match only already-recorded file changes. Never
    # parse a source URL out of assistant prose or arbitrary tool body text.
    legacy_tools, current = {}, "host"
    known = {os.path.normcase(item["path"]): item for item in rows(metadata.get("file_changes"))
        if isinstance(item, dict) and isinstance(item.get("path"), str)}
    for message in messages:
        if not isinstance(message, dict) or message.get("_is_meta") or message.get("_origin") in {"team", "subagent"}:
            continue
        if human_turn([message]) != "host":
            current = message["_uuid"]
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            identity = block.get("id") if block.get("type") == "tool_use" else block.get("tool_use_id")
            if not isinstance(identity, str):
                continue
            key = (current, identity)
            if block.get("type") == "tool_use" and current not in covered and (all_turns or current == selected):
                name = block.get("name") if isinstance(block.get("name"), str) else "tool"
                legacy_tools[key] = {"turn": current, "name": name, "input": block.get("input", {}), "error": True}
            elif block.get("type") == "tool_result" and key in legacy_tools:
                call = legacy_tools[key]
                call["error"] = bool(block.get("is_error"))
                values = call["input"]
                path = values.get("file_path") or values.get("notebook_path") if isinstance(values, dict) else None
                normalized = os.path.normcase(os.path.abspath(os.path.join(cwd, path))) if isinstance(path, str) else None
                if not call["error"] and call["name"] in WRITERS and normalized in known:
                    files[normalized] = {"path": known[normalized]["path"], "name": os.path.basename(known[normalized]["path"])}
    for call in legacy_tools.values():
        legacy = True
        row = tools.setdefault(call["name"], {"name": call["name"], "count": 0, "errors": 0})
        row["count"] += 1
        row["errors"] += call["error"]
    return {"version": 1, "scope": "conversation" if all_turns else "turn", "turnId": None if all_turns else selected,
        "files": list(files.values()), "sources": list(sources.values()), "tools": sorted(tools.values(), key=lambda row: row["name"]),
        "legacySourcesUnavailable": legacy}


def read_summary(cwd, session_id, *, turn_id=None, all_turns=False):
    if not isinstance(session_id, str) or not IDENTITY.fullmatch(session_id):
        raise ValueError("Invalid session ID")
    from xuloom.core.storage import data_directory, session_directory
    from xuloom.features.chat_links import _read
    root = data_directory() / "history"
    raw = _read(session_directory(cwd) / (session_id + ".json"), root)
    if raw is None:
        raise ValueError("会话不存在，可能已经移动或删除")
    if not isinstance(raw, (dict, list)) or isinstance(raw, dict) and (not isinstance(raw.get("messages", []), list) or not isinstance(raw.get("metadata", {}), dict)):
        raise ValueError("会话记录格式无效，无法读取摘要")
    if any(not isinstance(message, dict) for message in (raw if isinstance(raw, list) else raw.get("messages", []))):
        raise ValueError("会话正文格式无效，无法读取摘要")
    warnings = []
    if isinstance(raw, dict):
        metadata = raw.setdefault("metadata", {})
        if not isinstance(metadata.get("artifacts"), dict):
            metadata["artifacts"] = {}
        original = metadata["artifacts"].get("references", {})
        references = metadata["artifacts"]["references"] = {}
        merge_references(references, original)
        # A running child's latest receipt may be saved before its parent next
        # checkpoints. Read only registered workers owned by this conversation.
        workers = metadata.get("subagents", [])
        if not isinstance(workers, list):
            workers = []; warnings.append("子代理索引格式无效，未读取其来源")
        for worker in workers:
            if not isinstance(worker, dict):
                warnings.append("一个子代理索引条目格式无效，未读取其来源"); continue
            if worker.get("status") not in {"running", "interrupted"}:
                continue
            agent = worker.get("agent_id")
            if not isinstance(agent, str) or not IDENTITY.fullmatch(agent):
                continue
            try:
                child = _read(session_directory(cwd) / "subagents" / (agent + ".json"), root)
                child_metadata = child.get("metadata", {}) if isinstance(child, dict) else {}
                if not isinstance(child_metadata, dict) or not isinstance(child_metadata.get("delegation"), dict) or child_metadata["delegation"].get("owner_session_id") != session_id:
                    continue
                artifacts = child_metadata.get("artifacts", {})
                merge_references(references, artifacts.get("references", {}) if isinstance(artifacts, dict) else {}, agent_id=agent)
            except (ValueError, OSError):
                warnings.append("一个子代理的来源记录暂不可读；已保留其他记录")
    return {"sessionId": session_id, **project_summary(raw, cwd, turn_id=turn_id, all_turns=all_turns), "warnings": warnings}
