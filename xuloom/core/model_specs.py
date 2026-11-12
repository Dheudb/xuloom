"""Small, endpoint-scoped defaults from official model specifications.

Unknown gateway aliases remain unknown; explicit per-connection settings win.
Source: https://developers.openai.com/api/docs/models (checked 2026-11-05).
"""
import copy
from urllib.parse import urlsplit

OPENAI_MODELS = {
    'gpt-6-astra': False, 'gpt-6.1-sol': False,
    'gpt-6-sol': True, 'gpt-6-luna': True, 'gpt-6.1-terra': True,
}

# Native windows from publishers' model cards/config.json, checked 2026-11-12.
# Exact aliases only; a private fine-tune's base belongs in user configuration.
PUBLISHED_WINDOWS = {
    'qwen3.6-35b-a3b': 262144, 'qwen3.6-35b-a3b-fp8': 262144,
    'qwen3.8-27b-fp8': 262144, 'agents-a1': 262144,
    'intern-s1': 65536, 'intern-s2-preview-397b': 262144,
    'deepseek-v4-flash': 1048576, 'deepseek-v4-pro': 1048576, 'deepseek-v4.1-flash': 1048576,
    'glm-5.2': 1048576, 'glm-5.3': 1048576,
    'kimi-k2.6': 262144, 'minimax-m3': 1048576,
}


def metadata_window(row):
    """Accept declared total windows, never confuse output limits with context."""
    fields = ('context_window', 'context_length', 'max_model_len', 'max_context_length',
              'max_sequence_length', 'max_position_embeddings')
    values = []
    for value in [row] + [row.get(key) for key in ('limits', 'top_provider', 'architecture', 'text_config')]:
        if not isinstance(value, dict): continue
        values.extend(value[key] for key in fields if type(value.get(key)) is int and 4096 <= value[key] <= 16000000)
    return min(values) if values else None


def context_spec(provider, model, base_url):
    """Context defaults do not imply a model transport or reasoning adapter."""
    spec = official(provider, model, base_url)
    if spec is not None: return {**spec, 'source': 'official_spec'}
    url = urlsplit(base_url or '')
    paths = {'openai': {'', '/v1'}, 'anthropic': {'/anthropic', '/anthropic/v1'}}
    if (model in {'deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash-vision-exp'}
            and url.scheme == 'https' and url.netloc.lower() == 'api.deepseek.com'
            and url.path.rstrip('/') in paths.get(provider, set()) and not url.query and not url.fragment):
        return {'window': 1048576, 'source': 'official_spec'}
    # Public model names are a nominal fallback when a gateway omits metadata.
    # An explicit window (including one discovered from that server) wins.
    name = model.lower()
    if '/' in name and name.split('/', 1)[0] in {'qwen', 'internscience', 'internlm', 'deepseek-ai', 'zai-org', 'moonshotai', 'minimaxai'}:
        name = name.split('/', 1)[1]
    window = PUBLISHED_WINDOWS.get(name)
    return {'window': window, 'source': 'published_model'} if window else None


def official(provider, model, base_url):
    if provider != 'openai' or model not in OPENAI_MODELS:
        return None
    url = urlsplit(base_url or 'https://api.openai.com/v1')
    if (url.scheme != 'https' or url.netloc.lower() != 'api.openai.com'
            or url.path.rstrip('/') != '/v1' or url.query or url.fragment):
        return None
    return {'window': 1050000, 'maxOutput': 128000, 'transport': 'responses',
            'efforts': (['none'] if OPENAI_MODELS[model] else []) + ['low', 'medium', 'high', 'xhigh', 'max']}


def with_defaults(item):
    result = copy.deepcopy(item)
    for model in item.get('models', []):
        spec = context_spec(item['provider'], model, item.get('base_url', ''))
        if spec is None:
            continue
        if spec.get('transport') == 'responses':
            result.setdefault('model_inputs', {}).setdefault(model, ['text', 'image'])
            result.setdefault('model_reasoning', {}).setdefault(model, {
                'adapter': 'openai', 'efforts': spec['efforts'], 'canDisable': False,
                'thinking': 'default', 'effort': 'medium', 'budgetTokens': 2048})
        # A zero window means automatic; never replace an explicit user window.
        context = result.setdefault('model_context', {}).setdefault(model, {})
        context.update(window=context.get('window') or spec['window'], reserve=context.get('reserve', 0),
                       keepRecent=context.get('keepRecent', 0), backend=context.get('backend', 'summary'))
    return result
