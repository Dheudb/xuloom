"""One private connection can serve several explicitly configured media models."""
import copy
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from xuloom.core.config import load_config, update_config

KINDS = {"image", "image-edit", "video", "speech", "sound", "music"}
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){1,7}\Z")


def address(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Media API URL must be an HTTP(S) address without credentials, query or fragment")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
        if "%" in parsed.path or "//" in parsed.path or any(part in {".", ".."} for part in parsed.path.split("/")):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError("Invalid media API URL") from None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def origin(value):
    parsed = urlsplit(address(value))
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def validate(value):
    if not isinstance(value, dict) or value.keys() - {"id", "name", "protocol", "base_url", "network_mode", "api_key", "models", "download_origins"}:
        raise ValueError("Invalid media connection fields")
    if not isinstance(value.get("id"), str) or not IDENTITY.fullmatch(value["id"]):
        raise ValueError("Media connection ID must contain 1–64 letters, digits, _ or -")
    if value.get("protocol", "fal-queue") != "fal-queue":
        raise ValueError("Unsupported media protocol; select fal-queue")
    base_url = address(value.get("base_url"))
    name = value.get("name") or urlsplit(base_url).netloc
    if not isinstance(name, str) or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid media connection name")
    key = value.get("api_key", "")
    if not isinstance(key, str) or not key.isascii() or len(key) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("Invalid media API key")
    mode = value.get("network_mode", "direct")
    if mode not in {"direct", "system"}:
        raise ValueError("Media networking must be direct or system")
    models = value.get("models", [])
    if not isinstance(models, list) or len(models) > 64:
        raise ValueError("Media models must be an array with at most 64 entries")
    cleaned = []
    for item in models:
        if not isinstance(item, dict) or item.keys() - {"id", "kind", "model", "prompt_field", "image_field", "defaults"}:
            raise ValueError("Invalid media model fields")
        if not isinstance(item.get("id"), str) or not IDENTITY.fullmatch(item["id"]) or item.get("kind") not in KINDS:
            raise ValueError("Supply a media model ID and supported kind")
        if not isinstance(item.get("model"), str) or not MODEL.fullmatch(item["model"]):
            raise ValueError("Supply the exact fal model endpoint, such as owner/model/endpoint")
        prompt_field = item.get("prompt_field") or ("text" if item["kind"] in {"speech", "sound"} else "prompt")
        image_field = item.get("image_field") or "image_urls"
        if any(not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", field) for field in (prompt_field, image_field)):
            raise ValueError("Media input fields must be simple field names")
        defaults = item.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError("Media defaults must be a JSON object")
        try:
            encoded = json.dumps(defaults, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("Invalid media defaults JSON") from None
        if len(encoded.encode("utf-8")) > 32768:
            raise ValueError("Media defaults exceed 32 KB")
        if re.search(r'"(?:api[_-]?key|authorization|access[_-]?token|password|secret)"\s*:', encoded, re.I):
            raise ValueError("Store credentials in the connection API key, not model defaults")
        cleaned.append({"id": item["id"], "kind": item["kind"], "model": item["model"],
            "prompt_field": prompt_field, "image_field": image_field, "defaults": copy.deepcopy(defaults)})
    if len({item["id"] for item in cleaned}) != len(cleaned):
        raise ValueError("Duplicate media model IDs")
    downloads = value.get("download_origins", [])
    if not isinstance(downloads, list) or len(downloads) > 32:
        raise ValueError("Download origins must be an array")
    for url in downloads:
        address(url)
        if urlsplit(url).path not in {"", "/"}:
            raise ValueError("Download origins must not contain a path")
    return {"id": value["id"], "name": name, "protocol": "fal-queue", "api_key": key,
        "base_url": base_url, "network_mode": mode,
        "models": cleaned, "download_origins": sorted(set(downloads))}


def connections(cfg=None):
    rows = (load_config() if cfg is None else cfg).get("media_connections", [])
    if not isinstance(rows, list):
        raise ValueError("media_connections must be an array")
    result = [validate(row) for row in rows]
    if len({row["id"] for row in result}) != len(result):
        raise ValueError("Duplicate media connection IDs")
    return result


def public_connections(cfg=None):
    return [{**{key: value for key, value in row.items() if key != "api_key"}, "hasKey": bool(row["api_key"])}
        for row in connections(cfg)]


def save(value):
    if not isinstance(value, dict) or value.keys() - {"id", "name", "protocol", "base_url", "network_mode", "apiKey", "models", "download_origins"}:
        raise ValueError("Invalid media connection fields")
    if "id" not in value or value["id"] == "":
        value = {**value, "id": "media-" + uuid.uuid4().hex}
    def change(cfg):
        rows = connections(cfg)
        old = next((row for row in rows if row["id"] == value.get("id")), {})
        candidate = {**value, "api_key": value.get("apiKey") or old.get("api_key", "")}
        candidate.pop("apiKey", None)
        candidate = validate(candidate)
        cfg["media_connections"] = [candidate if row["id"] == candidate["id"] else row for row in rows]
        if not old:
            cfg["media_connections"].append(candidate)
        return cfg
    return public_connections(update_config(change))


def remove(identity):
    def change(cfg):
        rows = connections(cfg)
        if not any(row["id"] == identity for row in rows):
            raise ValueError("Unknown media connection")
        cfg["media_connections"] = [row for row in rows if row["id"] != identity]
        return cfg
    return public_connections(update_config(change))


def resolve(connection_id, model_id, *, frozen=None):
    matches = [(connection, model) for connection in connections() for model in connection["models"]
        if connection["id"] == connection_id and model["id"] == model_id]
    if len(matches) != 1:
        raise ValueError("Media connection/model is missing; configure it in Settings → Media generation")
    connection, model = matches[0]
    binding = {key: copy.deepcopy(connection[key]) for key in ("id", "protocol", "base_url", "network_mode", "download_origins")}
    binding["model"] = copy.deepcopy(model)
    if frozen is not None and binding != frozen:
        raise ValueError("Media target changed; restore the original connection/model before inspecting this task")
    if not connection["api_key"].strip():
        raise ValueError("Configure an API key for the media connection")
    return binding, connection["api_key"]


def revision(binding, key):
    return hashlib.sha256(json.dumps([binding, key], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
