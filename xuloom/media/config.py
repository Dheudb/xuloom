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
    protocol = value.get("protocol", "fal-queue")
    if protocol not in {"fal-queue", "auto"}:
        raise ValueError("Unsupported media protocol")
    base_url = address(value.get("base_url"))
    name = value.get("name") or urlsplit(base_url).netloc
    if not isinstance(name, str) or len(name) > 160 or any(ord(c) < 32 for c in name):
        raise ValueError("Invalid media connection name")
    key = value.get("api_key", "")
    from xuloom.security.credentials import is_protected
    if not is_protected(key) and (not isinstance(key, str) or not key.isascii() or len(key) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in key)):
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
        if not isinstance(item.get("model"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", item["model"]) or any(part in {".", "..", ""} for part in item["model"].split("/")):
            raise ValueError("Supply the provider's exact model name")
        if protocol == "fal-queue" and not MODEL.fullmatch(item["model"]):
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
    return {"id": value["id"], "name": name, "protocol": protocol, "api_key": key,
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
    from xuloom.security.credentials import is_protected
    return [{**{key: value for key, value in row.items() if key != "api_key"},
             "hasKey": bool(row["api_key"]) and not is_protected(row["api_key"]),
             **({"credentialUnavailable": True} if is_protected(row["api_key"]) else {})}
        for row in connections(cfg)]


def public_models(cfg=None):
    from xuloom.security.credentials import is_protected
    return [{"connectionId": row["id"], "id": model["id"], "kind": model["kind"],
        "model": model["model"], "base_url": row["base_url"], "hasKey": bool(row["api_key"]) and not is_protected(row["api_key"]),
        **({"credentialUnavailable": True} if is_protected(row["api_key"]) else {})}
        for row in connections(cfg) for model in row["models"]]


def save_model(value):
    """Three-field model form; keep old connection/job identities on edits."""
    if not isinstance(value, dict) or value.keys() - {"connectionId", "id", "kind", "model", "base_url", "apiKey"}:
        raise ValueError("Invalid generation model fields")
    if value.get("kind") not in KINDS:
        raise ValueError("Select a generation type")
    base = address(value.get("base_url"))
    host = urlsplit(base).hostname.lower()
    if host in {"api.openai.com", "api.elevenlabs.io"} and not urlsplit(base).path:
        base += "/v1"
    if host == "queue.fal.run" and isinstance(value.get("model"), str) and base.endswith("/" + value["model"].strip()):
        base = base[:-len(value["model"].strip()) - 1]
    def change(cfg):
        rows = connections(cfg)
        identity, model_id = value.get("connectionId"), value.get("id")
        old = next((r for r in rows if r["id"] == identity), None)
        if identity and (not old or not any(m["id"] == model_id for m in old["models"])):
            raise ValueError("Generation model no longer exists; refresh settings")
        key = value.get("apiKey") or (old["api_key"] if old else "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Fill in the model API key; unauthenticated local services may use a placeholder")
        model = {"id": model_id or "model-" + uuid.uuid4().hex, "kind": value["kind"], "model": value.get("model", "").strip()}
        if old:
            previous = next(m for m in old["models"] if m["id"] == model_id)
            changed = base != old["base_url"] or previous["model"] != model["model"] or previous["kind"] != model["kind"]
            if (changed or key != old["api_key"]) and len(old["models"]) > 1:
                # A flat model edit must not change its siblings' API or key.
                row = validate({**old, "id": "media-" + uuid.uuid4().hex, "base_url": base, "api_key": key,
                    "protocol": "auto" if changed else old["protocol"], "models": [model if changed else previous]})
                cfg["media_connections"] = [{**r, "models": [m for m in r["models"] if m["id"] != model_id]} if r["id"] == identity else r for r in rows] + [row]
                return cfg
            if not changed:
                model = previous  # Preserve existing jobs' frozen legacy binding.
            row = {**old, "base_url": base, "api_key": key,
                "protocol": "auto" if changed else old["protocol"],
                "models": [model if m["id"] == model_id else m for m in old["models"]]}
            cfg["media_connections"] = [validate(row) if r["id"] == identity else r for r in rows]
        else:
            # Model configuration stays flat; identical credentials can share private storage.
            shared = next((r for r in rows if r["base_url"] == base and r["api_key"] == key and r["protocol"] == "auto"), None)
            if shared:
                row = validate({**shared, "models": shared["models"] + [model]})
                cfg["media_connections"] = [row if r["id"] == shared["id"] else r for r in rows]
            else:
                row = validate({"id": "media-" + uuid.uuid4().hex, "protocol": "auto", "base_url": base, "api_key": key, "models": [model]})
                cfg["media_connections"] = rows + [row]
        return cfg
    return public_models(update_config(change))


def remove_model(connection_id, model_id):
    def change(cfg):
        rows = connections(cfg)
        row = next((r for r in rows if r["id"] == connection_id), None)
        if not row or not any(m["id"] == model_id for m in row["models"]):
            raise ValueError("Unknown generation model")
        updated = {**row, "models": [m for m in row["models"] if m["id"] != model_id]}
        cfg["media_connections"] = [updated if r["id"] == connection_id else r for r in rows if r["id"] != connection_id or updated["models"]]
        return cfg
    return public_models(update_config(change))


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
    from xuloom.security.credentials import require_secret
    key = require_secret(connection["api_key"])
    if not key.strip():
        raise ValueError("Configure an API key for the media connection")
    return binding, key


def revision(binding, key):
    return hashlib.sha256(json.dumps([binding, key], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
