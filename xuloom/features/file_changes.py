"""File-tool changes, independent of Git and kept out of model messages."""
import difflib
import os
import time
from pathlib import Path

SNAPSHOT_LIMIT = 200_000


def check_observed_file(context, path):
    """Compare the agent's last observed version with disk inside the mutation queue."""
    cache = context.get("file_state_cache")
    entry = cache.get_entry(path) if cache is not None else None
    if entry:
        current = cache.snapshot(path)
        if not current or not entry.get("sha256") or current.get("sha256") != entry["sha256"]:
            raise ValueError("File changed since this agent last read it. Read the current file before editing; no write was performed.")


def write_file_text(path, text, *, create_only=False, context=None):
    from xuloom.core.storage import atomic_text
    from xuloom.features.file_revisions import prepare_revision
    revision = prepare_revision(context or {}, path, text.encode("utf-8"))
    # Match normal write semantics for symlinks without replacing the link itself.
    atomic_text(os.path.realpath(path), text, preserve_mode=True, create_only=create_only)
    return revision


def record_written_file(context, path, before, operation, *, before_snapshot=None,
                        target_preview="", replacement_preview="", revision=None):
    """Observers run after the mutation; their failure must not invite a replay."""
    warnings = []
    def observe(name, callback):
        try:
            return callback()
        except Exception as error:
            warnings.append({"observer": name, "error": str(error)[:500]})
            return None
    cache = context.get("file_state_cache")
    snapshot = observe("file_state_cache", lambda: cache.record_write(path)) if cache is not None else None
    context.setdefault("read_file_state", {})[path] = True
    notify_change(context, path, before, read_snapshot(path), operation, revision)
    tracker = context.get("artifact_tracker")
    if tracker is not None:
        observe("artifact_tracker", lambda: tracker.record_file_change(path=path, operation=operation,
            before_snapshot=before_snapshot, after_snapshot=snapshot, target_preview=target_preview,
            replacement_preview=replacement_preview, agent_id=context.get("agent_id"), session_id=context.get("session_id")))
    verifier = context.get("verification_manager")
    if verifier is not None:
        observe("verification_manager", lambda: verifier.record_file_modification(path, operation))
    return snapshot, {"write_applied": True, **({"revision_id": revision} if revision else {}), **({"post_write_warnings": warnings,
        "retry_advice": "File was written. Do not repeat the mutation to repair observer records."} if warnings else {})}


def describe_change(path, before, after, operation="edit"):
    available = before is not None and after is not None
    record = {"path": str(path), "operation": operation, "updatedAt": int(time.time() * 1000),
              "added": None, "removed": None, "diff": "", "statsAvailable": available, "truncated": False}
    if available:
        left, right = before.splitlines(), after.splitlines()
        matcher = difflib.SequenceMatcher(None, left, right)
        added = removed = 0
        for tag, i, j, k, l in matcher.get_opcodes():
            if tag in {"replace", "delete"}: removed += j - i
            if tag in {"replace", "insert"}: added += l - k
        diff = "\n".join(difflib.unified_diff(left, right, fromfile="修改前", tofile="修改后", lineterm=""))
        record.update(added=added, removed=removed, diff=diff[:SNAPSHOT_LIMIT], truncated=len(diff) > SNAPSHOT_LIMIT)
    return record


def notify_change(context, path, before, after, operation, revision=None):
    callback = context.get("capability_event")
    if not callable(callback): return
    # Large files remain clickable, but unbounded snapshots never cross IPC.
    before = before if before is not None and len(before) <= SNAPSHOT_LIMIT else None
    after = after if after is not None and len(after) <= SNAPSHOT_LIMIT else None
    try:
        callback({"type": "file_changed", "change": {**describe_change(os.path.realpath(path), before, after, operation), **({"revisionId": revision} if revision else {})},
                  "before": before, "after": after, "agent_id": context.get("agent_id")})
    except Exception:
        # A UI observer must never turn an already successful write into a tool failure.
        pass


def read_snapshot(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = stream.read(SNAPSHOT_LIMIT + 1)
        return value if len(value) <= SNAPSHOT_LIMIT else None
    except (OSError, UnicodeError): return None


def legacy_changes(activity, cwd):
    files = {}
    for tool in activity:
        inputs = tool.get("input", {})
        path = inputs.get("file_path") or inputs.get("notebook_path")
        if tool.get("status") != "completed" or tool.get("name") not in {"file_write_tool", "file_edit_tool", "notebook_edit_tool"} or not path:
            continue
        path = str((Path(cwd) / path).resolve())
        created = "operation: create" in tool.get("result", "") or "Successfully created" in tool.get("result", "")
        after = inputs.get("content", inputs.get("new_string")) if created else None
        record = describe_change(path, "" if created else None, after, "create" if created else "edit")
        record["updatedAt"] = tool.get("finishedAt") or tool.get("startedAt") or 0
        record["legacy"] = True
        files[os.path.normcase(path)] = record
    return list(files.values())


def preview_path(cwd, requested, known):
    if not isinstance(requested, str) or not requested.strip() or "\0" in requested or "://" in requested:
        raise ValueError("请选择本地文件路径")
    path = (Path(cwd) / requested).resolve()
    allowed = {os.path.normcase(os.path.abspath(item["path"])) for item in known if item.get("path")}
    try: path.relative_to(Path(cwd).resolve())
    except ValueError:
        if os.path.normcase(str(path)) not in allowed: raise ValueError("只能查看当前工作目录或本会话实际修改的文件")
    if not path.is_file(): raise ValueError("文件已被移动、删除，或路径不是文件")
    return path


def preview_image_file(cwd, requested, known, base_path=None):
    from xuloom.context.tool_media import MAX_BYTES, preview_image_bytes
    if base_path:
        base = preview_path(cwd, base_path, known)
        if not isinstance(requested, str): raise ValueError("请选择本地图片")
        requested = str(base.parent / requested)
    path = preview_path(cwd, requested, known)
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        raise ValueError("不支持此图片格式")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    return {**preview_image_bytes(data), "name": path.name, "path": str(path)}


def preview_file(cwd, requested, known):
    path = preview_path(cwd, requested, known)
    with path.open("rb") as stream: data = stream.read(500_001)
    truncated = len(data) > 500_000
    data = data[:500_000]
    errors = "replace" if truncated else "strict"
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        content = data.decode("utf-32", errors=errors)
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        content = data.decode("utf-16", errors=errors)
    else:
        if b"\0" in data: raise ValueError("此文件是二进制文件，暂不支持文本预览")
        try: content = data.decode("utf-8-sig", errors=errors)
        except UnicodeError:
            if path.suffix.lower() not in {".txt", ".log", ".csv", ".tsv", ".md", ".xml", ".html", ".htm", ".json", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".rst"}:
                raise ValueError("此文件不是 UTF-8 文本，暂不支持预览") from None
            try: content = data.decode("gb18030", errors=errors)
            except UnicodeError: raise ValueError("无法识别此文本文件的编码") from None
    return {"path": str(path), "content": content, "truncated": truncated, "size": path.stat().st_size,
            "modifiedAt": int(path.stat().st_mtime * 1000)}
