"""Model adapter contract.

A :class:`ModelAdapter` connects QueryEngine to one model API. Xuloom ships
two native adapters (``openai`` for Chat Completions/Responses and
``anthropic`` for Messages). Hosts and plugins register more with
:func:`xuloom.models.register_adapter` and select them through
``EngineConfig(model_provider="<adapter name>")``.

Canonical formats exchanged with a custom adapter:

* ``request.messages`` – Anthropic-style history: ``{"role": "user"|"assistant",
  "content": [blocks]}`` where blocks are ``text``, ``image``, ``tool_use``
  (``id``, ``name``, ``input``) and ``tool_result`` (``tool_use_id``,
  ``content``, ``is_error``). :func:`to_openai_messages` converts it to the
  OpenAI Chat format for OpenAI-like APIs.
* ``request.tools`` – ``{"name", "description", "input_schema"}`` JSON-Schema
  definitions (see :func:`to_openai_tools`).
* the return value – built with :func:`model_response`: ``stop_reason`` is
  ``"end_turn"``, ``"tool_use"`` or ``"max_tokens"``; tool calls are executed
  by the engine afterwards with the normal permission checks and receipts.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional

__all__ = [
    "ModelAdapter",
    "ModelRequest",
    "ToolCall",
    "model_response",
    "to_openai_messages",
    "to_openai_tools",
]

STOP_REASONS = frozenset({"end_turn", "tool_use", "max_tokens"})


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""
    id: str
    name: str
    input: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRequest:
    """Everything a custom adapter needs for one model turn."""
    client: Any
    model: str
    system_prompt: str
    messages: List[dict]
    tools: List[dict]
    max_tokens: int
    turn: int
    thinking: Optional[dict] = None
    reasoning_effort: Optional[str] = None
    _emit: Optional[Callable[[dict], Awaitable[None]]] = field(default=None, repr=False)
    _engine: Any = field(default=None, repr=False)

    def openai_messages(self) -> List[dict]:
        """The history in OpenAI Chat format, system prompt first."""
        return to_openai_messages(self.messages, self.system_prompt, engine=self._engine)

    def openai_tools(self) -> List[dict]:
        return to_openai_tools(self.tools)

    async def emit_text(self, text: str) -> None:
        """Stream visible text to the UI as it arrives (optional)."""
        if text and self._emit is not None:
            await self._emit({"type": "text_delta", "turn": self.turn, "text": text})

    async def emit_status(self, status: str) -> None:
        """Report ``"connected"``, ``"reasoning"`` or ``"receiving"`` (optional)."""
        if self._emit is not None:
            await self._emit({"type": "model_status", "status": status, "turn": self.turn})


def model_response(*, text: str = "", tool_calls: Iterable[ToolCall] = (), stop_reason: Optional[str] = None,
                   input_tokens: int = 0, output_tokens: int = 0, cache_read_input_tokens: int = 0,
                   cache_creation_input_tokens: int = 0):
    """Build the response object the engine consumes."""
    calls = list(tool_calls or ())
    reason = stop_reason or ("tool_use" if calls else "end_turn")
    if reason not in STOP_REASONS:
        raise ValueError("stop_reason must be one of: " + ", ".join(sorted(STOP_REASONS)))
    if calls and reason != "tool_use":
        raise ValueError("A response with tool calls must use stop_reason='tool_use'")
    content = [SimpleNamespace(type="text", text=text)] if text else []
    for call in calls:
        if not isinstance(call, ToolCall):
            call = ToolCall(**dict(call))
        if not call.id or not call.name:
            raise ValueError("Tool calls need an id and a name")
        content.append(SimpleNamespace(type="tool_use", id=str(call.id), name=str(call.name), input=dict(call.input or {})))
    usage = SimpleNamespace(input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
                            cache_creation_input_tokens=int(cache_creation_input_tokens or 0),
                            cache_read_input_tokens=int(cache_read_input_tokens or 0))
    return SimpleNamespace(stop_reason=reason, content=content, usage=usage)


def to_openai_tools(tools: Iterable[Mapping[str, Any]]) -> List[dict]:
    """Canonical tool definitions -> OpenAI ``tools=[{"type": "function", ...}]``."""
    return [{"type": "function", "function": {"name": item.get("name", ""), "description": item.get("description", ""),
             "parameters": item.get("input_schema") or {"type": "object", "properties": {}}}} for item in tools]


def to_openai_messages(messages: List[dict], system_prompt: str = "", *, engine=None) -> List[dict]:
    """Canonical history -> OpenAI Chat messages.

    With an engine, its full converter is used (images, documents, reasoning
    fields). Without one, text, tool calls and tool results are converted.
    """
    if engine is not None:
        return engine._clean_roles_for_openai_api(messages, system_prompt)
    out = [{"role": "system", "content": system_prompt}] if system_prompt else []
    for message in messages:
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        texts = [block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
        if message.get("role") == "assistant":
            calls = [{"id": block["id"], "type": "function", "function": {"name": block["name"],
                      "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}}
                     for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
            item = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "\n".join(part.get("text", "") for part in result if isinstance(part, dict))
                out.append({"role": "tool", "tool_call_id": block.get("tool_use_id", ""), "content": str(result or "")})
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


class ModelAdapter:
    """Base class for model adapters. Subclass and override the documented methods."""

    #: Provider id selected with ``EngineConfig(model_provider=...)``. Lowercase.
    name: str = ""
    #: Used when the connection has no ``api_base_url``.
    default_base_url: str = ""
    #: Built-in adapters use the engine's native streaming path.
    native: bool = False

    def create_client(self, *, model: str, api_base_url: str, api_key: str, http_client) -> Any:
        """Return the client object passed back in ``ModelRequest.client``.

        ``http_client`` is an ``httpx.AsyncClient`` that already applies Xuloom's
        proxy mode and per-connection request limits; prefer it for HTTP APIs.
        The returned object must provide ``async close()`` and ``is_closed``;
        :class:`HttpModelClient` does this for plain HTTP adapters.
        """
        return HttpModelClient(http_client, base_url=api_base_url or self.default_base_url, api_key=api_key)

    async def stream_turn(self, request: ModelRequest):
        """Run one model turn and return :func:`model_response` (required for custom adapters)."""
        raise NotImplementedError(f"Model adapter {self.name!r} does not implement stream_turn")

    async def complete_text(self, client: Any, *, model: str, messages: List[dict], max_tokens: int) -> tuple:
        """Plain text completion used for summaries and memory extraction.

        ``messages`` use ``{"role", "content": str}``. Return ``(text, usage)``
        where usage is ``{"input_tokens": int, "output_tokens": int}``. The
        default implementation reuses :meth:`stream_turn` without tools.
        """
        canonical = [{"role": item["role"], "content": [{"type": "text", "text": str(item["content"])}]} for item in messages]
        response = await self.stream_turn(ModelRequest(client=client, model=model, system_prompt="", messages=canonical,
                                                       tools=[], max_tokens=max_tokens, turn=0))
        text = "".join(getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text")
        usage = getattr(response, "usage", None)
        return text, {"input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                      "output_tokens": int(getattr(usage, "output_tokens", 0) or 0)}

    def is_transient_error(self, error: BaseException) -> bool:
        """True when the fallback model should retry the turn (rate limits, 5xx, connection)."""
        status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
        return isinstance(status, int) and (status == 429 or status >= 500)

    def is_context_overflow_error(self, error: BaseException) -> bool:
        """True when the request exceeded the context window (engine compacts and retries)."""
        return False


class HttpModelClient:
    """Minimal client for HTTP adapters: base URL, key and a managed ``httpx`` client."""

    def __init__(self, http_client, *, base_url: str, api_key: str):
        self.http = http_client
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key

    @property
    def is_closed(self) -> bool:
        return bool(getattr(self.http, "is_closed", False))

    async def close(self) -> None:
        result = self.http.aclose()
        if inspect.isawaitable(result):
            await result

    def url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")
