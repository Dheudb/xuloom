"""Durable external request receipts inside existing central tool artifacts."""
import re

from xuloom.context.tool_media import artifact_owner
from xuloom.core.storage import artifact_directory, atomic_json, data_directory

JOB_ID = re.compile(r"media-[a-f0-9]{32}\Z")
PHASES = {"submitting", "submitted", "unknown", "downloaded", "failed", "cancelled", "cancel_requested", "result_pending", "result_unavailable", "downloading", "download_failed"}


class JobNotOwned(ValueError):
    pass


def directory(context):
    root = data_directory() / "history"
    folder = artifact_directory(context["session_cwd"]) / artifact_owner(context) / "media-jobs"
    folder.resolve().relative_to(root.resolve())
    probe = folder
    while probe != root:
        if probe.is_symlink() or getattr(probe, "is_junction", lambda: False)():
            raise ValueError("Linked media task storage is not allowed")
        probe = probe.parent
    return folder


def save(context, record):
    if not JOB_ID.fullmatch(record.get("id", "")):
        raise ValueError("Invalid media task identity")
    folder = directory(context)
    path = folder / (record["id"] + ".json")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Linked media task record is not allowed")
    atomic_json(path, record)


def load(context, identity):
    if not isinstance(identity, str) or not JOB_ID.fullmatch(identity):
        raise ValueError("Invalid media task ID")
    from xuloom.features.chat_links import _read
    record = _read(directory(context) / (identity + ".json"), data_directory() / "history")
    if not isinstance(record, dict) or record.get("id") != identity or record.get("session_id") != artifact_owner(context):
        raise ValueError("Unknown media task in this conversation")
    if not isinstance(record.get("binding"), dict) or not isinstance(record.get("agent_id"), str) or not isinstance(record.get("phase"), str) or record["phase"] not in PHASES or type(record.get("created_at")) is not int:
        raise ValueError("Invalid saved media task receipt")
    if context.get("parent_agent_id") and record.get("agent_id") != context.get("agent_id"):
        raise JobNotOwned("This media task belongs to another agent")
    return record


def public(record):
    fields = ("id", "kind", "connection_id", "model_id", "request_id", "phase", "provider_status", "error", "files", "created_at", "updated_at", "agent_id")
    return {key: record[key] for key in fields if key in record}


def listing(context, *, strict=False):
    folder = directory(context)
    if not folder.exists():
        return []
    result = []
    for file in sorted(folder.glob("media-*.json"), reverse=True):
        try:
            result.append(public(load(context, file.stem)))
        except JobNotOwned:
            continue
        except (OSError, ValueError):
            if strict:
                raise ValueError("A saved media receipt is unreadable. No new generation was submitted; recover the existing record before retrying.") from None
            result.append({"id": file.stem, "phase": "unreadable", "error": "任务记录无法读取；不能据此认定任务未提交，请检查集中历史。"})
    return sorted(result, key=lambda row: row.get("created_at", 0), reverse=True)


def uncertain_duplicate(context, binding, digest):
    for row in listing(context, strict=True):
        if row.get("phase") not in {"submitting", "unknown"}:
            continue
        record = load(context, row["id"])
        if record.get("binding") == binding and record.get("input_digest") == digest:
            return record
    return None
