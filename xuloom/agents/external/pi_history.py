"""Verify Pi's append-only history against its RPC export, not a second context engine."""
import hashlib
import json
import os

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import IOPath
from .host import LIMIT, PREFIX, SOURCE_ID
from .runtime import ExternalExecutionError, tool_result_digest


def verify_file(path, state, exported):
    path = IOPath(path)
    entries, leaf = exported.get("entries"), exported.get("leafId")
    if not isinstance(entries, list) or not entries or leaf != entries[-1].get("id"):
        raise ExternalExecutionError("external_history_changed", "External history has no matching durable leaf")
    try:
        with path.open("r+b") as stream:
            header = json.loads(stream.readline())
            header_end = stream.tell()
            offset = state.get("history_offset", header_end)
            if header.get("type") != "session" or header.get("id") != state["session_id"] or header.get("version") != 3:
                raise ValueError("External history header changed")
            size = stream.seek(0, os.SEEK_END)
            if type(offset) is not int or not header_end <= offset <= size or size - offset > LIMIT:
                raise ValueError("External history offset changed")
            stream.seek(0)
            digest = hashlib.sha256()
            while stream.tell() < offset:
                digest.update(stream.read(min(1024 * 1024, offset - stream.tell())))
            if state.get("history_sha256") and digest.hexdigest() != state["history_sha256"]:
                raise ValueError("External history prefix changed during execution")
            tail = stream.read()
            digest.update(tail)
            disk = [json.loads(line) for line in tail.split(b"\n") if line.strip()]
            if disk != entries: raise ValueError("External history differs from its RPC export")
            os.fsync(stream.fileno())
        return {"history_sha256": digest.hexdigest(), "history_offset": size, "entry_cursor": leaf}
    except ExternalExecutionError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise PersistenceError("External history could not be verified and saved; the task was not made resumable") from error


def current_messages(exported, initial_leaf, base_cursor):
    entries = exported["entries"]
    start = next((i + 1 for i, entry in enumerate(entries) if entry.get("id") == initial_leaf), None)
    if start is None and initial_leaf is not None and initial_leaf != base_cursor:
        raise ExternalExecutionError("external_history_changed", "External history lost the owned starting cursor")
    start = start or 0
    return [entry["message"] for entry in entries[start:] if entry.get("type") == "message"]


def verify_receipts(host, messages):
    calls, results = {}, {}
    for message in messages:
        if message.get("role") == "assistant":
            for block in message.get("content", []):
                if block.get("type") == "toolCall":
                    identity = block.get("id")
                    if identity in calls: raise ValueError("External history repeats a tool identity")
                    arguments = dict(block["arguments"])
                    if not block["name"].startswith(PREFIX) or arguments.pop(SOURCE_ID, None) != identity:
                        raise ValueError("External history lost its owned tool identity")
                    calls[identity] = (block["name"][len(PREFIX):], arguments)
        elif message.get("role") == "toolResult":
            identity = message["toolCallId"]
            if identity in results: raise ValueError("External history repeats a tool receipt")
            results[identity] = message
    if calls != host.calls or set(results) != set(calls) or host.started_calls != set(host.receipts) or host.engine.in_progress_tools:
        raise ExternalExecutionError("external_outcome_unknown", "External and native tool completion could not be reconciled")
    for identity, result in results.items():
        if identity in host.receipts:
            if not result["toolName"].startswith(PREFIX): raise ValueError("External receipt changed its tool")
            proof = (result["toolName"][len(PREFIX):], tool_result_digest(result["content"], result["isError"]))
            if proof != host.receipts[identity]:
                raise ExternalExecutionError("external_outcome_unknown", "External history did not acknowledge the native tool result")
        elif result.get("isError") is not True:
            raise ExternalExecutionError("external_outcome_unknown", "An unexecuted tool was not recorded as rejected")
