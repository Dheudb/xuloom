"""Explicit per-model reasoning controls translated to the selected wire protocol."""
import copy

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ADAPTERS = {
    "openai": "openai", "deepseek": "openai", "qwen": "openai",
    "qwen-local": "openai", "anthropic": "anthropic", "anthropic-budget": "anthropic",
}


def normalize_profile(value, provider):
    if not isinstance(value, dict) or value.keys() - {"adapter", "efforts", "canDisable", "thinking", "effort", "budgetTokens"}:
        raise ValueError("推理配置字段无效")
    adapter = value.get("adapter")
    if not isinstance(adapter, str) or ADAPTERS.get(adapter) != provider:
        raise ValueError("推理参数格式与 API 协议不匹配")
    efforts = value.get("efforts", [])
    if not isinstance(efforts, list) or any(item not in EFFORTS for item in efforts):
        raise ValueError("请选择有效的推理强度")
    efforts = list(dict.fromkeys(efforts))
    if adapter == "anthropic-budget" and efforts:
        raise ValueError("固定 Thinking 预算模式不同时发送 effort")
    toggle = value.get("canDisable", adapter != "openai")
    if type(toggle) is not bool or adapter == "openai" and toggle:
        raise ValueError("Thinking 开关能力无效；OpenAI effort 格式通过 none 档关闭（仅模型支持时）")
    thinking = value.get("thinking", "default")
    if not isinstance(thinking, str) or thinking not in {"default", "enabled", "disabled"} or adapter == "openai" and thinking != "default":
        raise ValueError("此模型不支持所选 Thinking 设置")
    if thinking == "default" and adapter != "openai":
        thinking = "enabled"  # Legacy preference; the UI now has an explicit on/off switch.
    if thinking == "disabled" and not toggle:
        raise ValueError("此模型未声明支持关闭 Thinking")
    effort = value.get("effort", "default")
    if effort != "default" and effort not in efforts:
        raise ValueError("推理强度不在此模型已声明的可选范围内")
    if adapter != "openai" and "none" in efforts:
        raise ValueError("Thinking 开关与强度分别配置；none 仅用于 OpenAI effort 格式")
    budget = value.get("budgetTokens", 2048)
    if type(budget) is not int or not 1024 <= budget <= 1000000:
        raise ValueError("Thinking token 预算必须为 1024–1000000 的整数")
    return {"adapter": adapter, "efforts": efforts, "canDisable": toggle,
            "thinking": thinking, "effort": effort, "budgetTokens": budget}


def configured_profile(provider, model, base_url):
    from xuloom.core.config import load_config, normalize_api_base_url
    from xuloom.core.model_catalog import active_connection, connections
    cfg = load_config()
    active = active_connection(cfg)
    matches = [item for item in connections(cfg) if item["provider"] == provider
        and normalize_api_base_url(provider, item.get("base_url", "")) == normalize_api_base_url(provider, base_url or "")
        and model in item.get("models", [])]
    selected = next((item for item in matches if item["id"] == active["id"]), None)
    selected = selected or (matches[0] if len(matches) == 1 else None)
    value = (selected or {}).get("model_reasoning", {}).get(model)
    if value is None:
        from xuloom.core.model_specs import with_defaults
        value = with_defaults({'provider': provider, 'models': [model], 'base_url': base_url}).get('model_reasoning', {}).get(model)
    return normalize_profile(value, provider) if value is not None else None


def request_options(profile, provider, *, effort_override=None, max_tokens=None):
    """No profile means no guessed parameters; callers retain legacy explicit APIs."""
    if profile is None:
        return {}
    profile = normalize_profile(profile, provider)
    adapter, thinking = profile["adapter"], profile["thinking"]
    effort = effort_override if effort_override is not None else profile["effort"]
    if effort != "default" and effort not in profile["efforts"]:
        raise ValueError("当前模型不支持指定的推理强度；未向接口发送不兼容参数")
    options = {}
    if adapter == "openai":
        return {"reasoning_effort": effort} if effort != "default" else {}
    if thinking == "disabled":
        effort = "default"  # Never send enabled-effort alongside a disabled thinking toggle.
    if adapter in {"anthropic", "anthropic-budget"}:
        if thinking != "default":
            config = {"type": "disabled" if thinking == "disabled" else "adaptive" if adapter == "anthropic" else "enabled"}
            if config["type"] == "enabled":
                budget = profile["budgetTokens"]
                if max_tokens is not None and budget >= max_tokens:
                    raise ValueError("Thinking 预算必须小于本次模型响应上限")
                config["budget_tokens"] = budget
            options["thinking"] = config
        if effort != "default":
            options["output_config"] = {"effort": effort}
        return options
    extra = {}
    if thinking != "default":
        enabled = thinking == "enabled"
        if adapter == "deepseek":
            extra["thinking"] = {"type": thinking}
        elif adapter == "qwen":
            extra["enable_thinking"] = enabled
        else:
            extra["chat_template_kwargs"] = {"enable_thinking": enabled}
    if effort != "default":
        options["reasoning_effort"] = effort
    if extra:
        options["extra_body"] = extra
    return copy.deepcopy(options)


def transport_reasoning(engine, value):
    """Provider-returned protocol state; never replay to another route or model."""
    if not isinstance(value, dict) or not isinstance(value.get("content"), str) or getattr(engine, "model_provider", None) != "openai":
        return None
    from xuloom.core.request_flow import connection_id
    model = engine.tool_context.get("request_model", engine.primary_model)
    if value.get("model") != model or value.get("connection") != connection_id(engine.api_base_url or "", engine._api_key):
        return None
    return {key: value[key] for key in ("connection", "model", "content")}
