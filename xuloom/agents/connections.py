"""Resolve role connections without putting credentials into worker records."""
import copy

from xuloom.core.config import load_config, normalize_api_base_url
from xuloom.core.model_catalog import connections
from xuloom.core.network import model_network_mode
from xuloom.core.reasoning import normalize_profile


def resolve_connection(identity, model=None, *, frozen=None):
    """Return a public frozen binding and private SDK options, never change env."""
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
        raise ValueError("A valid model connection ID is required")
    matches = [item for item in connections(load_config()) if item["id"] == identity]
    if len(matches) != 1:
        raise ValueError("Model connection is missing or ambiguous: " + identity)
    item = matches[0]
    model = model or item.get("model")
    if not model or model not in item["models"]:
        raise ValueError("Model is not available in connection: " + identity)
    from xuloom.security.credentials import api_key as checked_key
    key = checked_key(item.get("api_key", ""))
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Configure an API key for model connection: " + identity)
    provider = item["provider"]
    inputs = item.get("model_inputs", {}).get(model)
    if inputs is not None and (not isinstance(inputs, list) or any(kind not in {"text", "image", "document"} for kind in inputs)):
        raise ValueError("Invalid model input contract in connection: " + identity)
    profile = item.get("model_reasoning", {}).get(model)
    profile = normalize_profile(profile, provider) if profile is not None else None
    binding = {"connection_id": identity, "provider": provider, "model": model,
        "api_base_url": normalize_api_base_url(provider, item.get("base_url", "")),
        "network_mode": model_network_mode(item.get("network_mode", "direct")),
        "contract": {"inputs": copy.deepcopy(inputs), "reasoning": profile}}
    from xuloom.tools.native_search import normalize as normalize_search
    search = normalize_search(item.get('native_search'))
    if search['backend'] != 'builtin' or frozen is not None and 'search' in frozen.get('contract', {}):
        binding['contract']['search'] = search
    if frozen is not None and binding != frozen:
        raise ValueError("Worker model connection changed; restore its original settings or start a new worker")
    return binding, {"api_key": key, "network_mode": binding["network_mode"]}
