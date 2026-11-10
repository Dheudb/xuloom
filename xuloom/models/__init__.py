"""Model adapters: connect QueryEngine to any model API.

See :mod:`xuloom.models.adapter` for the contract and ``docs/model-adapters.md``
for a complete example.
"""
from xuloom.models.adapter import (
    HttpModelClient,
    ModelAdapter,
    ModelRequest,
    ToolCall,
    model_response,
    to_openai_messages,
    to_openai_tools,
)
from xuloom.models.registry import adapter_names, get_adapter, is_native_provider, register_adapter, unregister_adapter

__all__ = [
    "HttpModelClient", "ModelAdapter", "ModelRequest", "ToolCall", "model_response", "to_openai_messages",
    "to_openai_tools", "adapter_names", "get_adapter", "is_native_provider", "register_adapter", "unregister_adapter",
]
