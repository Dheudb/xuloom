"""Model requests: streaming or adapter responses, fallback detection, VCR serialization and output limits."""
import asyncio
import inspect
import os
import sys
from xuloom.core.model_stream import AbortRequestedError
from xuloom.core.engine_parts.loop import _drain_cancelled
from xuloom.core.engine_state import EngineState


class ModelSamplingMixin(EngineState):
    """Model requests: streaming or adapter responses, fallback detection, VCR serialization and output limits."""

    def _serialize_response_usage(self, response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        fields = [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]
        payload = {}
        for field in fields:
            value = getattr(usage, field, None)
            if value is not None:
                payload[field] = value
        return payload

    def _serialize_response_for_vcr(self, response) -> dict:
        return {
            "stop_reason": getattr(response, "stop_reason", None),
            "content": [
                block
                for block in [self._block_to_dict(item) for item in getattr(response, "content", []) or []]
                if block
            ],
            "usage": self._serialize_response_usage(response),
            **({"_openai_reasoning": response._openai_reasoning} if getattr(response, "_openai_reasoning", None) is not None else {}),
        }

    async def _replay_vcr_response_events(self, response, *, turn_count: int, event_callback=None):
        await self._emit_stream_event(event_callback, {
            "type": "message_start",
            "turn": turn_count,
            "replayed": True,
        })
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")
                if text:
                    await self._emit_stream_event(event_callback, {
                        "type": "text_delta",
                        "turn": turn_count,
                        "text": text,
                        "replayed": True,
                    })
            elif getattr(block, "type", "") == "tool_use":
                await self._emit_stream_event(event_callback, {
                    "type": "tool_scheduled",
                    "turn": turn_count,
                    "tool_name": getattr(block, "name", ""),
                    "tool_use_id": getattr(block, "id", None),
                    "replayed": True,
                })
        await self._emit_stream_event(event_callback, {
            "type": "message_stop",
            "turn": turn_count,
            "stop_reason": getattr(response, "stop_reason", None),
            "replayed": True,
        })

    def _get_model_max_output_tokens(self, model: str = "") -> tuple:
        from xuloom.core.config import get_model_max_output_tokens
        return get_model_max_output_tokens(model or self.primary_model)

    def _get_max_output_tokens(self) -> int:
        """
        Resolve max output tokens: explicit env override, delegated response cap,
        then the ordinary conversation default. Reservations still enforce all
        remaining budgets before a delegated request reaches the provider.
        """
        from xuloom.core.config import DEFAULT_CAPPED_MAX_TOKENS
        env_val = os.environ.get("XULOOM_MAX_OUTPUT_TOKENS", "")
        if env_val:
            try:
                parsed = int(env_val)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
        default_tokens, _ = self._get_model_max_output_tokens()
        from xuloom.agents.collaboration import DELEGATION_BUDGET
        delegation = DELEGATION_BUDGET.get()
        if delegation is not None:
            return min(default_tokens, delegation["request_cap"])
        return min(default_tokens, DEFAULT_CAPPED_MAX_TOKENS)

    def _is_model_fallback_error(self, error: Exception) -> bool:
        if self.network_mode == "auto":
            return False  # The HTTP transport owns retries and preserves uncertain outcomes.
        adapter = self._model_adapter()
        if adapter is not None and not adapter.native and adapter.is_transient_error(error):
            return True
        # Inspect only SDKs already loaded by the selected connection.
        for name in ("anthropic", "openai"):
            sdk = sys.modules.get(name)
            transient = tuple(getattr(sdk, kind) for kind in ("APIConnectionError", "RateLimitError", "InternalServerError")
                if isinstance(getattr(sdk, kind, None), type))
            if isinstance(error, transient):
                return True
            status = getattr(sdk, "APIStatusError", None)
            if isinstance(status, type) and isinstance(error, status):
                return getattr(error, "status_code", None) in {500, 502, 503, 504, 529}

        rendered = str(error).lower()
        markers = [
            "overloaded",
            "service unavailable",
            "temporarily unavailable",
            "rate limit",
            "connection error",
        ]
        return any(marker in rendered for marker in markers)

    async def _stream_model_response(
        self, **kwargs,
    ):
        # A draft generated before outstanding evidence is collected is process
        # commentary, even when the provider ends that model message normally.
        workers = self.tool_context["subagent_tasks"]
        provisional = any(not task.done() for task in workers.tasks.values()) or bool(set(workers.results) - workers.delivered)
        callback = kwargs.get("event_callback")
        if provisional and callable(callback):
            async def commentary(event):
                if event.get("type") in {"message_start", "text_delta"}:
                    event = {**event, "channel": "commentary"}
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            kwargs["event_callback"] = commentary
        if self.tool_context.get("external_backend"):
            from xuloom.agents.external.runtime import sample
            return await sample(self, **kwargs)
        from xuloom.core.output_limits import model_reservation
        from xuloom.context.compaction import output_limit, policy
        kwargs["max_tokens"] = output_limit(self, kwargs)
        async with model_reservation(self, kwargs["max_tokens"]) as grant:
            kwargs["max_tokens"] = grant["maximum"]
            from xuloom.core.model_specs import official
            adapter = self._model_adapter()
            if adapter is not None and not adapter.native:
                response, entries, fallback = await self._receive_adapter_response(adapter, **kwargs)
            elif policy(self)["backend"] == "openai-responses" or official(self.model_provider, kwargs['model_name'], self.api_base_url):
                from xuloom.context.responses import receive
                response, entries, fallback = await receive(self, **kwargs)
            else:
                if self.native_context: raise ValueError("Native compacted history requires its declared Responses connection")
                response, entries, fallback = await self._receive_model_response(**kwargs)
            usage = self._serialize_response_usage(response)
            from xuloom.context.compaction import calibrate
            calibrate(self, kwargs, usage)
            grant["reported"] = int(self._serialize_response_usage(response).get("output_tokens", 0) or 0)
            return response, entries, fallback

    async def _receive_adapter_response(self, adapter, *, system_prompt, api_messages, tools_schema, turn_count,
                                        model_name, max_tokens, thinking_config=None, sys_print_callback=print,
                                        event_callback=None):
        """Run one turn through a registered (non-native) ModelAdapter.

        Tool calls are executed by the loop after the response, so no streaming
        tool entries are returned.
        """
        from xuloom.models.adapter import ModelRequest, STOP_REASONS
        self.tool_context["request_model"] = model_name

        streamed = []

        async def emit(event):
            if event.get("type") == "text_delta":
                streamed.append(True)
            await self._emit_stream_event(event_callback, event)

        request = ModelRequest(client=self.client, model=model_name, system_prompt=system_prompt,
                               messages=api_messages, tools=tools_schema, max_tokens=max_tokens, turn=turn_count,
                               thinking=thinking_config if self._thinking_config_explicit else None,
                               reasoning_effort=self.reasoning_effort if model_name == self.primary_model else None,
                               _emit=emit, _engine=self)
        await emit({"type": "model_status", "status": "waiting", "turn": turn_count})
        await emit({"type": "message_start", "turn": turn_count})
        task = asyncio.ensure_future(adapter.stream_turn(request))
        abort = asyncio.ensure_future(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait({task, abort}, return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                raise AbortRequestedError("Abort requested while waiting for the model adapter.")
            response = task.result()
        finally:
            # Every exit path (stop request, outer cancellation, errors) ends the
            # adapter call too; an orphaned request must not keep running.
            pending = [item for item in (task, abort) if not item.done()]
            for item in pending:
                item.cancel()
            if pending:
                await _drain_cancelled(pending)
        if getattr(response, "stop_reason", None) not in STOP_REASONS or not isinstance(getattr(response, "content", None), list):
            raise TypeError(f"Model adapter {adapter.name!r} must return xuloom.models.model_response(...)")
        if not streamed:
            text = "".join(getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text")
            if text:
                await emit({"type": "text_delta", "turn": turn_count, "text": text})
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                await emit({"type": "tool_scheduled", "turn": turn_count, "tool_name": block.name, "tool_use_id": block.id})
        await emit({"type": "message_stop", "turn": turn_count, "stop_reason": response.stop_reason})
        return response, [], False
