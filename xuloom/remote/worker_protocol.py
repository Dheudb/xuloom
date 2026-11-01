"""Credential-free, idempotent identity and frozen role for a native SSH worker."""
import copy
import hashlib
import json
import re
import uuid

VERSION = 2
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ROLE_FIELDS = {"agent_type", "agent_role", "system_prompt", "critical_reminder", "allowed_tools", "disallowed_tools",
    "skills", "model", "connection", "reasoning_effort", "web_search", "mcp_servers", "read_only", "omit_claude_md", "backend", "backend_program"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owner(value):
    if not isinstance(value, dict) or set(value) != {"token", "parent_session_id", "parent_agent_id", "agent_id", "depth"}:
        raise ValueError("Invalid remote worker ownership fields")
    if not isinstance(value["token"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["token"]):
        raise ValueError("Invalid remote worker control identity")
    for field in ("parent_session_id", "parent_agent_id", "agent_id"):
        if not isinstance(value[field], str) or not IDENTITY.fullmatch(value[field]):
            raise ValueError("Invalid remote worker " + field)
    if type(value["depth"]) is not int or not 1 <= value["depth"] <= 2:
        raise ValueError("Invalid remote worker depth")
    return copy.deepcopy(value)


def session_id(ownership):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "xuloom-native-ssh-worker:" + owner(ownership)["token"]))


def freeze_role(value):
    if not isinstance(value, dict) or value.keys() - ROLE_FIELDS:
        raise ValueError("Unsupported remote role fields; credentials and local execution settings are not transferred")
    result = copy.deepcopy(value)
    for key in ("agent_type", "agent_role", "system_prompt", "critical_reminder", "model", "connection", "backend_program"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError("Invalid remote role " + key)
    if result.get("backend", "native") not in {"native", "claude", "pi", "codex"}:
        raise ValueError("Unsupported remote execution backend")
    for key in ("allowed_tools", "disallowed_tools", "skills", "mcp_servers"):
        if key not in result or result[key] is None:
            continue
        if isinstance(result[key], (set, frozenset, tuple)):
            result[key] = sorted(result[key])
        if not isinstance(result[key], list) or not all(isinstance(item, str) and item for item in result[key]):
            raise ValueError("Invalid remote role " + key)
    for key in ("read_only", "omit_claude_md"):
        if key in result and type(result[key]) is not bool:
            raise ValueError("Invalid remote role " + key)
    from xuloom.core.reasoning import EFFORTS
    if result.get("reasoning_effort") is not None and result["reasoning_effort"] not in EFFORTS:
        raise ValueError("Invalid remote role reasoning effort")
    if result.get("web_search", "inherit") not in {"inherit", "enabled", "disabled"}:
        raise ValueError("Invalid remote role web search policy")
    if result.get("model") == "inherit":
        result["model"] = None
    if not result.get("system_prompt"):
        raise ValueError("Remote role requires actual instructions")
    return result


def open_request(value):
    if not isinstance(value, dict) or value.keys() - {"owner", "role", "context"} or not {"owner", "role"} <= value.keys():
        raise ValueError("Invalid remote worker open request")
    owned, role = owner(value["owner"]), freeze_role(value["role"])
    context = value.get("context")
    if context is not None and (not isinstance(context, list) or not all(isinstance(item, dict) and item.get("role") in {"user", "assistant"} for item in context)):
        raise ValueError("Invalid remote worker reference context")
    return {"owner": owned, "role": role, "context": copy.deepcopy(context)}


def binding(value):
    """Store an ownership hash on the host, never its control token or API key."""
    value = open_request(value)
    return {"version": VERSION, "session_id": session_id(value["owner"]), "owner_digest": digest(value["owner"]),
        "role_digest": digest({"role": value["role"], "context": value["context"]}),
        "agent_id": value["owner"]["agent_id"], "parent_agent_id": value["owner"]["parent_agent_id"],
        "source_session_id": value["owner"]["parent_session_id"], "logical_depth": value["owner"]["depth"]}
