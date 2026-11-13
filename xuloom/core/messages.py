"""
Message envelope system with UUID tracking.

Every message in the conversation carries a stable UUID, timestamp, and
metadata so that abort/rollback/replay/transcript operations can target
precise points in the conversation history.

Mirrors Claude Code's utils/messages.ts message wrapper pattern.
"""

import time
import uuid as _uuid
from typing import Any, Dict, List, Optional, Union


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_user_message(
    content: Union[str, list],
    *,
    msg_uuid: Optional[str] = None,
    is_meta: bool = False,
    is_continuation: bool = False,
    origin: str = "human",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap a user-role message with UUID metadata."""
    return {
        "role": "user",
        "content": content,
        "_uuid": msg_uuid or str(_uuid.uuid4()),
        "_timestamp": timestamp or _now_iso(),
        "_is_meta": is_meta,
        "_is_continuation": is_continuation,
        "_origin": origin,
    }


def original_user_message(messages):
    """Find the user task a continuation belongs to, excluding supplemental input."""
    return next((message for message in reversed(messages) if message.get("role") == "user"
        and message.get("_origin", "human") == "human" and not message.get("_is_meta")
        and not message.get("_is_tool_result") and not message.get("_steering_id")
        and not any(block.get("type") == "tool_result" for block in message.get("content", []) if isinstance(block, dict))), None)


def create_assistant_message(
    content: Any,
    *,
    msg_uuid: Optional[str] = None,
    model: str = "",
    stop_reason: str = "",
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap an assistant-role message with UUID metadata."""
    return {
        "role": "assistant",
        "content": content,
        "_uuid": msg_uuid or str(_uuid.uuid4()),
        "_timestamp": timestamp or _now_iso(),
        "_model": model,
        "_stop_reason": stop_reason,
    }


def create_tool_result_message(
    tool_results: list,
    *,
    msg_uuid: Optional[str] = None,
    source_assistant_uuid: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap a tool_result user message with UUID linking back to the assistant turn."""
    return {
        "role": "user",
        "content": tool_results,
        "_uuid": msg_uuid or str(_uuid.uuid4()),
        "_timestamp": timestamp or _now_iso(),
        "_is_tool_result": True,
        "_source_assistant_uuid": source_assistant_uuid,
    }


def get_msg_uuid(msg: dict) -> Optional[str]:
    """Extract UUID from a message, if present."""
    return msg.get("_uuid")


def derive_short_id(msg_uuid: str) -> str:
    """Derive a 6-char short ID from a message UUID (for display/referencing)."""
    hex_part = msg_uuid.replace("-", "")[:10]
    return format(int(hex_part, 16) % (36**6), "06x")


def strip_internal_fields(msg: dict) -> dict:
    """Return a copy with only API-facing fields (role, content)."""
    return {"role": msg["role"], "content": msg["content"]}


def strip_all_internal(messages: List[dict]) -> List[dict]:
    """Strip internal metadata from all messages for API submission."""
    return [strip_internal_fields(m) for m in messages]


def find_message_index_by_uuid(messages: List[dict], target_uuid: str) -> int:
    """Find the index of a message by UUID. Returns -1 if not found."""
    for i, msg in enumerate(messages):
        if msg.get("_uuid") == target_uuid:
            return i
    return -1


def rollback_to_uuid(messages: List[dict], target_uuid: str) -> List[dict]:
    """
    Remove all messages after (and including) the one with target_uuid.
    Returns the removed messages for potential undo.
    """
    idx = find_message_index_by_uuid(messages, target_uuid)
    if idx < 0:
        return []
    removed = messages[idx:]
    del messages[idx:]
    return removed


def rollback_incomplete_turn(messages: List[dict]) -> List[dict]:
    """
    Find the last assistant message with tool_use blocks that have no
    matching tool_result, and remove it plus any subsequent messages.
    Returns the removed messages.
    """
    if not messages:
        return []

    tool_result_ids = set()
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    tool_result_ids.add(tid)

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        has_unmatched = False
        for block in content:
            block_dict = block if isinstance(block, dict) else {}
            btype = block_dict.get("type") or getattr(block, "type", "")
            bid = block_dict.get("id") or getattr(block, "id", "")
            if btype == "tool_use" and bid and bid not in tool_result_ids:
                has_unmatched = True
                break

        if has_unmatched:
            target_uuid = msg.get("_uuid")
            if target_uuid:
                return rollback_to_uuid(messages, target_uuid)
            removed = messages[i:]
            del messages[i:]
            return removed

    return []


def get_conversation_snapshot(messages: List[dict]) -> List[str]:
    """Return list of UUIDs for all messages (for snapshot/restore)."""
    return [msg.get("_uuid", "") for msg in messages]


def export_messages_state(messages: List[dict]) -> List[dict]:
    """Export messages with UUID metadata for session persistence."""
    exported = []
    for msg in messages:
        entry = {"role": msg["role"]}
        content = msg.get("content")
        if isinstance(content, str):
            entry["content"] = content
        elif isinstance(content, list):
            serialized = []
            for block in content:
                if isinstance(block, dict):
                    serialized.append(block)
                elif hasattr(block, "__dict__"):
                    serialized.append({
                        k: v for k, v in block.__dict__.items()
                        if not k.startswith("_")
                    })
                else:
                    serialized.append({"type": "text", "text": str(block)})
            entry["content"] = serialized
        else:
            entry["content"] = str(content) if content else ""

        for key in ("_uuid", "_timestamp", "_is_meta", "_is_continuation",
                     "_origin", "_model", "_stop_reason", "_is_tool_result",
                     "_source_assistant_uuid"):
            if key in msg:
                entry[key] = msg[key]
        exported.append(entry)
    return exported
