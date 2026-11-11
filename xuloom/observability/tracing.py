"""Spans for runs, model requests and tool calls, sent to pluggable exporters.

The engine opens one ``run`` span per task; every model request (``model``)
and tool call (``tool``) inside it is a child span. Subagents share their
parent's tracer, so their runs nest under the tool call that started them.

Attribute names follow the OpenTelemetry GenAI conventions where one exists
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens`` ...), so the JSONL
file and an OpenTelemetry backend show the same fields.

An exporter is any object with ``export(span)``; ``on_start(span)`` and
``shutdown()`` are optional. Exporter errors are logged once and never stop a
run.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

__all__ = ["Span", "TraceExporter", "Tracer", "JsonlTraceExporter", "current_span"]

_logger = logging.getLogger("xuloom.observability")
_current: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar("xuloom_current_span", default=None)


def current_span() -> Optional["Span"]:
    """The innermost open span of the running task, if any."""
    return _current.get()


@dataclass
class Span:
    name: str
    kind: str
    """``"run"``, ``"model"`` or ``"tool"``."""
    trace_id: str
    span_id: str
    parent_id: Optional[str] = None
    start_time: float = 0.0
    """Seconds since the epoch."""
    end_time: Optional[float] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    """``"ok"`` or ``"error"``."""
    error: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return round((self.end_time - self.start_time) * 1000, 3)

    def set(self, **attributes) -> None:
        self.attributes.update({key: value for key, value in attributes.items() if value is not None})

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "trace_id": self.trace_id, "span_id": self.span_id,
                "parent_id": self.parent_id, "start_time": self.start_time, "end_time": self.end_time,
                "duration_ms": self.duration_ms, "status": self.status, "error": self.error,
                "attributes": dict(self.attributes)}


class TraceExporter:
    """Optional base class; any object with ``export(span)`` works."""

    def on_start(self, span: Span) -> None:  # pragma: no cover - default no-op
        pass

    def export(self, span: Span) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:  # pragma: no cover - default no-op
        pass


class Tracer:
    """Creates spans and hands finished ones to every exporter."""

    def __init__(self, exporters: Iterable[Any] = ()):
        self.exporters: List[Any] = []
        self._failed: set = set()
        for exporter in exporters:
            self.add_exporter(exporter)

    @property
    def enabled(self) -> bool:
        return bool(self.exporters)

    def add_exporter(self, exporter) -> None:
        if not callable(getattr(exporter, "export", None)):
            raise TypeError("Trace exporters need an export(span) method")
        if exporter not in self.exporters:
            self.exporters.append(exporter)

    def remove_exporter(self, exporter) -> None:
        if exporter in self.exporters:
            self.exporters.remove(exporter)

    def start(self, name: str, kind: str, *, parent: Optional[Span] = None, **attributes) -> Span:
        parent = parent if parent is not None else _current.get()
        span = Span(name=name, kind=kind, trace_id=parent.trace_id if parent else uuid.uuid4().hex,
                    span_id=uuid.uuid4().hex[:16], parent_id=parent.span_id if parent else None,
                    start_time=time.time())
        span.set(**attributes)
        for exporter in list(self.exporters):
            if callable(getattr(exporter, "on_start", None)):
                self._call(exporter, "on_start", span)
        return span

    def end(self, span: Span, *, error: Optional[BaseException] = None, **attributes) -> None:
        if span.end_time is not None:
            return
        span.set(**attributes)
        if error is not None:
            span.status = "error"
            span.error = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        span.end_time = time.time()
        for exporter in list(self.exporters):
            self._call(exporter, "export", span)

    @contextmanager
    def activate(self, span: Span) -> Iterator[Span]:
        """Make ``span`` the parent of spans started in this task until the block exits."""
        token = _current.set(span)
        try:
            yield span
        finally:
            _current.reset(token)

    @contextmanager
    def span(self, name: str, kind: str, **attributes) -> Iterator[Span]:
        """Start, activate and end a span around a block; exceptions mark it as an error."""
        span = self.start(name, kind, **attributes)
        try:
            with self.activate(span):
                yield span
        except BaseException as error:
            self.end(span, error=error)
            raise
        self.end(span)

    def shutdown(self) -> None:
        for exporter in list(self.exporters):
            if callable(getattr(exporter, "shutdown", None)):
                self._call(exporter, "shutdown")

    def _call(self, exporter, method: str, *args) -> None:
        try:
            getattr(exporter, method)(*args)
        except Exception as error:
            key = (id(exporter), method)
            if key not in self._failed:
                self._failed.add(key)
                _logger.warning("Trace exporter %s.%s failed: %s", type(exporter).__name__, method, error)


class JsonlTraceExporter(TraceExporter):
    """Append one JSON object per finished span to ``path``."""

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self._lock = threading.Lock()

    def export(self, span: Span) -> None:
        line = json.dumps(span.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def __repr__(self) -> str:
        return f"JsonlTraceExporter({self.path!r})"
