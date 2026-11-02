"""Named API connections with multiple model IDs and a legacy CLI projection."""
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from xuloom.core.config import normalize_api_base_url


def model_ids(values):
    if not isinstance(values, list) or len(values) > 1000:
        raise ValueError("模型列表必须是数组，最多 1000 个模型")
    result = []
    for value in values:
        if not isinstance(value, str) or len(value.strip()) > 256 or not value.strip():
            raise ValueError("请填写有效的模型 ID")
        value = value.strip()
        if value not in result:
            result.append(value)
    return result


def connections(cfg):
    saved = cfg.get("model_connections") or []
    if not isinstance(saved, list):
        raise ValueError("model_connections 必须是数组")
    result = [dict(item, models=model_ids(item.get("models", []))) for item in saved
              if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("provider") in {"openai", "anthropic"}]
    if result or "model_connections" in cfg:
        return result
    active = cfg.get("model_provider") or os.environ.get("XULOOM_MODEL_PROVIDER", "anthropic")
    for provider in ("openai", "anthropic"):
        model = cfg.get(provider + "_model") or os.environ.get(provider.upper() + "_MODEL", "")
        base = cfg.get(provider + "_base_url") or os.environ.get(provider.upper() + "_BASE_URL", "")
        key = cfg.get(provider + "_api_key") or os.environ.get(provider.upper() + "_API_KEY", "")
        if provider == active or model or base or key:
            result.append({"id": "legacy-" + provider, "name": "OpenAI 兼容 API" if provider == "openai" else "Anthropic",
                           "provider": provider, "base_url": base, "api_key": key, "model": model,
                           "models": model_ids(cfg.get(provider + "_models", [model] if model else [])),
                           "network_mode": cfg.get("network_mode", "direct")})
    return result


def active_connection(cfg):
    items = connections(cfg)
    if not items:
        return {"id": "", "name": "", "provider": cfg.get("model_provider") if cfg.get("model_provider") in {"openai", "anthropic"} else "openai",
                "model": "", "models": [], "base_url": "", "api_key": "", "network_mode": "direct"}
    return next((item for item in items if item["id"] == cfg.get("active_model_connection")), None) or next(
        (item for item in items if item["provider"] == (cfg.get("model_provider") or os.environ.get("XULOOM_MODEL_PROVIDER", "anthropic"))), items[0])


def public_connection(item):
    from xuloom.security.credentials import is_protected
    return {"connectionId": item["id"], "connectionName": item.get("name", item["id"]), "provider": item["provider"],
            "model": item.get("model", ""), "models": item.get("models", []), "baseUrl": item.get("base_url", ""),
            "hasKey": bool(item.get("api_key")) and not is_protected(item.get("api_key")),
            "credentialUnavailable": is_protected(item.get("api_key")), "networkMode": item.get("network_mode", "direct"),
            "modelInputs": item.get("model_inputs", {}), "modelReasoning": item.get("model_reasoning", {})}


def public_config(cfg):
    from xuloom.security.credentials import storage_status
    active = active_connection(cfg)
    return {**public_connection(active), "connections": [public_connection(item) for item in connections(cfg)],
            "credentialStorage": storage_status(cfg)}


def apply_connection_env(item):
    from xuloom.security.credentials import api_key as checked_key
    provider = item["provider"]
    for suffix in ("model", "base_url", "api_key"):
        variable = provider.upper() + "_" + suffix.upper()
        if item.get(suffix):
            os.environ[variable] = checked_key(item[suffix]) if suffix == "api_key" else item[suffix]
        else:
            os.environ.pop(variable, None)
    os.environ["XULOOM_MODEL_PROVIDER"] = provider
    os.environ["XULOOM_NETWORK_MODE"] = item.get("network_mode", "direct")


def apply_catalog_env(cfg):
    """Drop obsolete in-process credentials/projections before applying a saved catalog."""
    for provider in ("OPENAI", "ANTHROPIC"):
        for suffix in ("MODEL", "BASE_URL", "API_KEY", "FALLBACK_MODEL"):
            os.environ.pop(provider + "_" + suffix, None)
            os.environ.pop("XULOOM_" + provider + "_" + suffix, None)
    apply_connection_env(active_connection(cfg))


def project_active(cfg, item, items):
    cfg.update(model_connections=items, active_model_connection=item["id"], model_provider=item["provider"],
               network_mode=item.get("network_mode", "direct"))
    for suffix in ("model", "base_url", "api_key", "models"):
        cfg[item["provider"] + "_" + suffix] = item.get(suffix, [] if suffix == "models" else "")
    return cfg


def save_connection(cfg, params):
    cfg = dict(cfg)
    provider = params.get("provider")
    if provider not in {"openai", "anthropic"}:
        raise ValueError("Unsupported protocol")
    model = str(params.get("model", "")).strip()
    if not model:
        raise ValueError("Model ID is required")
    base = normalize_api_base_url(provider, params.get("baseUrl", ""))
    mode = params.get("networkMode", "direct")
    if mode not in {"direct", "system"}:
        raise ValueError("Unknown network mode")
    items = connections(cfg)
    active = active_connection(cfg)
    identity = params.get("connectionId") or (active["id"] if active["provider"] == provider and active["id"] else "legacy-" + provider)
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity):
        raise ValueError("API 连接 ID 无效")
    previous = next((item for item in items if item["id"] == identity), {})
    key = params.get("apiKey") or (previous.get("api_key", "") if previous.get("provider") == provider else "")
    from xuloom.security.credentials import api_key as checked_key
    key = checked_key(key)
    if not key:
        raise ValueError("请填写此 API 的 Key；本地无鉴权服务可填 not-needed")
    names = model_ids(params.get("models", previous.get("models", [])))
    if model not in names:
        names.append(model)
    inputs = dict(previous.get("model_inputs", {}))
    if "modelInputs" in params:
        values = params["modelInputs"]
        if not isinstance(values, dict) or any(name not in names or not isinstance(kinds, list)
            or "text" not in kinds or any(kind not in {"text", "image", "document"} for kind in kinds)
            or provider == "openai" and "document" in kinds
            for name, kinds in values.items()):
            raise ValueError("modelInputs 必须按模型 ID 配置 text/image/document 数组且包含 text；原生 document 目前仅支持 Anthropic 协议")
        inputs = {name: list(dict.fromkeys(kinds)) for name, kinds in values.items()}
    inputs = {name: kinds for name, kinds in inputs.items() if name in names}
    from xuloom.core.reasoning import normalize_profile
    profiles = params.get("modelReasoning", previous.get("model_reasoning", {}))
    if not isinstance(profiles, dict) or any(name not in names for name in profiles):
        raise ValueError("推理能力必须按此连接已配置的模型 ID 保存")
    profiles = {name: normalize_profile(value, provider) for name, value in profiles.items()}
    item = {"id": identity, "name": str(params.get("connectionName") or previous.get("name") or "我的 API").strip()[:100],
            "provider": provider, "model": model, "models": names, "base_url": base,
            "api_key": key, "network_mode": mode, "model_inputs": inputs, "model_reasoning": profiles}
    items = [item if old["id"] == identity else old for old in items]
    if not previous:
        items.append(item)
    return project_active(cfg, item, items)


def select_reasoning(cfg, params):
    from xuloom.core.reasoning import normalize_profile
    active = active_connection(cfg)
    if active["id"] != params.get("connectionId") or active.get("model") != params.get("model"):
        raise ValueError("当前 API 或模型已变化，请刷新后重试")
    profiles = dict(active.get("model_reasoning", {}))
    previous = profiles.get(active["model"])
    if previous is None:
        raise ValueError("请先在模型设置中声明此模型的推理参数格式与能力")
    if params.keys() - {"connectionId", "model", "thinking", "effort", "session"}:
        raise ValueError("未知的推理设置")
    updated = {**previous, **{key: params[key] for key in ("thinking", "effort") if key in params}}
    profiles[active["model"]] = normalize_profile(updated, active["provider"])
    item = {**active, "model_reasoning": profiles}
    return project_active(dict(cfg), item, [item if old["id"] == item["id"] else old for old in connections(cfg)])


def delete_connection(cfg, identity):
    items = connections(cfg)
    if not isinstance(identity, str) or not any(item["id"] == identity for item in items):
        raise ValueError("API 连接不存在，请刷新设置后重试")
    active = active_connection(cfg)
    remaining = [item for item in items if item["id"] != identity]
    result = dict(cfg)
    for provider in ("openai", "anthropic"):
        for suffix in ("model", "models", "base_url", "api_key"):
            result.pop(provider + "_" + suffix, None)
    selected = next((item for item in remaining if item["id"] == active["id"]), None)
    selected = selected or next((item for item in remaining if item.get("model") and item.get("api_key")), None)
    selected = selected or (remaining[0] if remaining else active_connection({"model_connections": [], "model_provider": active["provider"]}))
    # Retain legacy CLI projections only for connections still in the catalog.
    for provider in ("openai", "anthropic"):
        projection = next((item for item in remaining if item["provider"] == provider), None)
        if projection:
            for suffix in ("model", "models", "base_url", "api_key"):
                result[provider + "_" + suffix] = projection.get(suffix, [] if suffix == "models" else "")
    return project_active(result, selected, remaining)


def select_model(cfg, identity, model):
    items = connections(cfg)
    item = next((item for item in items if item["id"] == identity), None)
    if not item or model not in item.get("models", []):
        raise ValueError("请从此 API 已配置的模型列表中选择")
    if not item.get("api_key"):
        raise ValueError("请先在设置中填写此 API 的 Key")
    item["model"] = model
    return project_active(dict(cfg), item, items)


def sync_legacy_connection(cfg):
    """Keep CLI wizard edits in the named connection, preserving its other models."""
    provider = cfg.get("model_provider", "anthropic")
    active = active_connection(cfg)
    identity = active["id"] if active["provider"] == provider else "legacy-" + provider
    return save_connection(cfg, {"connectionId": identity, "provider": provider, "model": cfg.get(provider + "_model", ""),
        "baseUrl": cfg.get(provider + "_base_url", ""), "apiKey": cfg.get(provider + "_api_key", ""), "networkMode": cfg.get("network_mode", "direct")})


async def discover_models(cfg, params):
    items = connections(cfg)
    previous = next((item for item in items if item["id"] == params.get("connectionId")), None)
    provider = params.get("provider") or (previous or active_connection(cfg))["provider"]
    if provider not in {"openai", "anthropic"}:
        raise ValueError("Unsupported protocol")
    base = normalize_api_base_url(provider, params.get("baseUrl", (previous or {}).get("base_url", "")))
    base = base or ("https://api.openai.com/v1" if provider == "openai" else "https://api.anthropic.com/v1")
    key = params.get("apiKey", "")
    if not key and previous and normalize_api_base_url(provider, previous.get("base_url", "")) == base and previous["provider"] == provider:
        key = previous.get("api_key", "")
    if not key:
        raise ValueError("请填写 API Key；修改 API 地址后请重新填写 Key，或先保存连接")
    from xuloom.security.credentials import api_key as checked_key
    key = checked_key(key)
    headers = {"Authorization": "Bearer " + key} if provider == "openai" else {"x-api-key": key, "anthropic-version": "2023-06-01"}
    parsed = urlsplit(base)
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/models", parsed.query, ""))
    names = []
    mode = params.get("networkMode", (previous or {}).get("network_mode", "direct"))
    try:
        async with httpx.AsyncClient(trust_env=mode == "system", timeout=30, follow_redirects=False) as client:
            after = None
            for _ in range(20):
                response = await client.get(url, headers=headers, params={"after_id": after} if after else None)
                if response.status_code != 200:
                    raise ValueError("获取模型列表失败（HTTP %s），请检查地址、Key 和服务权限；也可以手动填写模型 ID" % response.status_code)
                payload = response.json()
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise ValueError("此服务未返回标准模型列表，请手动填写模型 ID")
                names.extend(row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
                             and row.get("type", "chat") not in {"embedding", "embeddings", "rerank", "image", "audio"})
                if provider != "anthropic" or not payload.get("has_more"):
                    break
                next_id = payload.get("last_id")
                if not next_id or next_id == after:
                    raise ValueError("模型分页异常，请手动填写模型 ID")
                after = next_id
            else:
                raise ValueError("模型列表过长，请手动填写需要的模型 ID")
    except (httpx.HTTPError, TypeError, KeyError):
        raise ValueError("无法连接模型列表接口，请检查连接方式和 API 地址；也可以手动填写模型 ID") from None
    except json.JSONDecodeError:
        raise ValueError("此服务未返回 JSON 模型列表，请手动填写模型 ID") from None
    return {"models": model_ids(names)}
