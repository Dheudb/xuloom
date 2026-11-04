"""Selected model bindings travel over SSH; credentials never enter run indexes."""
import copy
import json
from pathlib import Path

from xuloom.agents.connections import resolve_connection
from xuloom.core.config import load_config, normalize_api_base_url
from xuloom.core.model_catalog import active_connection
from xuloom.core.request_flow import connection_id, limits
from xuloom.core.storage import IOPath, atomic_json
from xuloom.security.credentials import api_key, transform


def quota(binding, key):
    default = "https://api.openai.com/v1" if binding["provider"] == "openai" else "https://api.anthropic.com"
    return connection_id(binding.get("api_base_url") or default, key)


def export(node):
    if node.get("api_source", "inherit") == "remote": return None
    config = load_config()
    entries = node.get("api_pool", []) if node.get("api_source") == "local" else [{"connection": active_connection(config)["id"], "model": None}]
    if not entries: raise ValueError("Choose at least one saved model connection for this node")
    result = []
    for index, entry in enumerate(entries):
        binding, options = resolve_connection(entry["connection"], entry.get("model"))
        result.append({"id": "api-" + str(index), "binding": binding, "api_key": options["api_key"],
            "network_mode": options["network_mode"], "limits": limits(config.get("model_request_limits")),
            "backend": entry.get("backend") or node.get("backend", "native")})
    return validate(result)


def validate(rows):
    from .store import identity
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64: raise ValueError("Choose 1–64 model bindings")
    result = []
    for row in rows:
        if not isinstance(row, dict): raise ValueError("Invalid model binding")
        binding = copy.deepcopy(row.get("binding"))
        if not isinstance(binding, dict) or binding.get("provider") not in {"openai", "anthropic"} or not isinstance(binding.get("contract"), dict):
            raise ValueError("Invalid model contract")
        if not all(isinstance(binding.get(k), str) and binding[k].strip() for k in ("connection_id", "model")):
            raise ValueError("Choose a connection and model")
        if normalize_api_base_url(binding["provider"], binding.get("api_base_url")) != binding.get("api_base_url"):
            raise ValueError("Model address is not normalized")
        key = api_key(row.get("api_key"))
        if not key.strip(): raise ValueError("Model credential is required")
        if row.get("backend", "native") not in {"native", "codex", "claude", "pi", "dsh"}: raise ValueError("Invalid model executor")
        result.append({"id": identity(row["id"]), "binding": binding, "api_key": key,
            "network_mode": row.get("network_mode", "direct"), "limits": limits(row.get("limits")), "backend": row.get("backend", "native")})
    if len({r["id"] for r in result}) != len(result): raise ValueError("Duplicate model binding")
    return result


def public(rows):
    return [{k: copy.deepcopy(v) for k, v in row.items() if k != "api_key"} | {"quota": quota(row["binding"], row["api_key"])} for row in rows]


def write(path, rows):
    path = IOPath(path)
    # Reuse the existing credential envelope and storage policy, with a known
    # model_connections layout. No global model configuration is changed.
    atomic_json(path, transform({"model_connections": validate(rows)}, Path(path), protect=True))
    if __import__("os").name != "nt": path.chmod(0o600)


def read(path):
    path = IOPath(path)
    value = transform(json.loads(path.read_text(encoding="utf-8")), Path(path))
    return validate(value["model_connections"])
