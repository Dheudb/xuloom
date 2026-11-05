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
        spec = official(item['provider'], model, item.get('base_url', ''))
        if spec is None:
            continue
        result.setdefault('model_inputs', {}).setdefault(model, ['text', 'image'])
        result.setdefault('model_reasoning', {}).setdefault(model, {
            'adapter': 'openai', 'efforts': spec['efforts'], 'canDisable': False,
            'thinking': 'default', 'effort': 'medium', 'budgetTokens': 2048})
        # A zero window means automatic; never replace an explicit user window.
        context = result.setdefault('model_context', {}).setdefault(model, {})
        context.update(window=context.get('window') or spec['window'], reserve=context.get('reserve', 0),
                       keepRecent=context.get('keepRecent', 0), backend=context.get('backend', 'summary'))
    return result
