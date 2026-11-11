"""Model adapters for tests and offline runs: a scripted mock and a cassette replayer."""
from __future__ import annotations

import copy
import itertools
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

from xuloom.models.adapter import ModelAdapter, ModelRequest, ToolCall, model_response

__all__ = ["ScriptedModel", "ReplayModel", "ResponseRecorder", "ScriptExhausted", "load_cassette", "response_from_record"]

_counter = itertools.count(1)


class ScriptExhausted(RuntimeError):
    """The engine asked for more model responses than the script or cassette holds."""


class ScriptedModel(ModelAdapter):
    """Answer each model request with the next scripted step.

    A step is a ``model_response(...)`` object, an exception to raise, or a callable
    ``step(request)`` (sync or async) returning a response. Every request is kept in
    ``requests`` for assertions::

        model = ScriptedModel([model_response(tool_calls=[ToolCall("1", "file_read_tool", {"file_path": "a"})]),
                               model_response(text="done")])
        with model:                                   # registers the adapter, removes it afterwards
            async with QueryEngine(model.config(cwd=tmp)) as engine:
                result = await engine.run("read a")
        assert len(model.requests) == 2

    ``default`` (optional) answers requests after the script is used up; without it the
    engine gets :class:`ScriptExhausted`.
    """

    default_base_url = "http://127.0.0.1:9"

    def __init__(self, script: Iterable[Any] = (), *, name: Optional[str] = None, default: Any = None):
        self.name = name or f"scripted-{next(_counter)}"
        self.script: List[Any] = list(script)
        self.default = default
        self.requests: List[ModelRequest] = []
        self._lock = threading.Lock()

    def _next_step(self):
        with self._lock:
            if self.script:
                return self.script.pop(0)
        if self.default is not None:
            return self.default
        raise ScriptExhausted(f"{type(self).__name__} {self.name!r} has no response left for request #{len(self.requests)}")

    async def stream_turn(self, request: ModelRequest):
        self.requests.append(request)
        step = self._next_step()
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step(request)
            if hasattr(step, "__await__"):
                step = await step
        return step

    # ---- registration helpers -------------------------------------------------
    def install(self) -> "ScriptedModel":
        from xuloom.models.registry import register_adapter
        register_adapter(self, replace=True)
        return self

    def uninstall(self) -> None:
        from xuloom.models.registry import get_adapter, unregister_adapter
        if get_adapter(self.name) is self:
            unregister_adapter(self.name)

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.uninstall()

    def config(self, base=None, **changes):
        """An :class:`EngineConfig` that uses this adapter (``base`` fields are kept)."""
        from xuloom.core.engine_config import EngineConfig
        config = base or EngineConfig()
        return config.replace(model=changes.pop("model", None) or config.model or "scripted", model_provider=self.name,
                              api_key=config.api_key or "scripted", network_mode=config.network_mode or "direct", **changes)


def response_from_record(record: dict):
    """Turn a recorded response (``engine._serialize_response_for_vcr`` shape) into a model response."""
    content = record.get("content") or []
    text = "".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
    calls = [ToolCall(str(block.get("id", "")), str(block.get("name", "")), dict(block.get("input") or {}))
             for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
    stop = record.get("stop_reason")
    if stop not in {"end_turn", "tool_use", "max_tokens"}:
        stop = "tool_use" if calls else "end_turn"
    usage = record.get("usage") or {}
    return model_response(text=text, tool_calls=calls, stop_reason=stop,
                          input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
                          cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                          cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0))


def load_cassette(path: Union[str, os.PathLike]) -> List[dict]:
    """Read the response records of a cassette written by ``record_responses``."""
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: not a JSON line ({error.msg})") from error
            if "xuloom_cassette" in row:
                continue
            if "purpose" in row:
                if not isinstance(row.get("purpose"), str) or not isinstance(row.get("text"), str):
                    raise ValueError(f"{path}:{number}: auxiliary records need 'purpose' and 'text'")
            elif not isinstance(row.get("response"), dict):
                raise ValueError(f"{path}:{number}: missing 'response'")
            records.append(row)
    return records


class ReplayModel(ScriptedModel):
    """Replay a recorded run in order, without network access or API keys.

    Record with ``EngineConfig(record_responses="run.jsonl")`` (or ``XULOOM_RECORD_RESPONSES``),
    then run the same task with ``ReplayModel("run.jsonl").config(...)``. Responses are
    served in recorded order, so replays stay valid when the system prompt, paths or dates
    change; a run that asks for more responses than were recorded fails with
    :class:`ScriptExhausted` instead of reaching a real model. Only the top-level engine is
    recorded: replay runs without subagents (``collaboration_mode="solo"``).

    Compaction and memory requests are recorded with their purpose and replayed from their
    own sequence (through the engine), so they never consume a model turn's response.
    """

    def __init__(self, cassette: Union[str, os.PathLike, Iterable[dict]], *, name: Optional[str] = None):
        records = load_cassette(cassette) if isinstance(cassette, (str, os.PathLike)) else list(cassette)
        self.records = records
        self.auxiliary: dict = {}
        for row in records:
            if "purpose" in row:
                self.auxiliary.setdefault(row["purpose"], []).append(row)
        super().__init__([response_from_record(row["response"]) for row in records if "purpose" not in row],
                         name=name or f"replay-{next(_counter)}")

    def replay_auxiliary(self, purpose: str):
        """The next recorded ``(text, usage)`` for ``purpose`` (called by the engine's auxiliary gate)."""
        with self._lock:
            queue = self.auxiliary.get(purpose) or []
            if not queue:
                raise ScriptExhausted(f"Replay {self.name!r} has no recorded {purpose!r} response left")
            row = queue.pop(0)
        return row["text"], dict(row.get("usage") or {})

    async def complete_text(self, client, *, model, messages, max_tokens):
        # Without a purpose this request cannot be matched to a recording; refusing keeps the
        # model-turn sequence aligned (the default would consume the next turn's response).
        raise ScriptExhausted("ReplayModel answers summaries only through the engine's auxiliary gate")


class ResponseRecorder:
    """Append every live model response of one engine to a JSONL cassette."""

    def __init__(self, path: Union[str, os.PathLike], *, model: str = "", provider: str = ""):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"xuloom_cassette": 1, "model": model, "provider": provider}) + "\n")

    def append(self, *, turn: int, model: str, response: dict, session_id: str = "") -> None:
        self._write({"turn": turn, "model": model, "session_id": session_id, "response": copy.deepcopy(response)})

    def append_auxiliary(self, *, purpose: str, model: str, text: str, usage: dict, session_id: str = "") -> None:
        self._write({"purpose": purpose, "model": model, "session_id": session_id, "text": text,
                     "usage": dict(usage or {})})

    def _write(self, row: dict) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
