"""OpenTelemetry exporter (optional dependency: ``pip install xuloom[otel]``).

Langfuse, Jaeger, Honeycomb, Grafana Tempo and others accept OTLP, so this one
exporter covers them; point the standard ``OTEL_EXPORTER_OTLP_*`` variables at
the backend.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from xuloom.observability.tracing import Span, TraceExporter

__all__ = ["OpenTelemetryTraceExporter", "otlp_exporter"]


def _ns(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


def _attribute(value: Any):
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(item, (bool, int, float, str)) for item in value):
        return list(value)
    return str(value)


class OpenTelemetryTraceExporter(TraceExporter):
    """Mirror Xuloom spans as OpenTelemetry spans, keeping their parent/child structure.

    ``tracer_provider`` defaults to the global provider configured by the host
    application (``opentelemetry.trace.set_tracer_provider``).
    """

    def __init__(self, tracer_provider=None, *, instrumentation_name: str = "xuloom"):
        from opentelemetry import trace
        self._trace = trace
        provider = tracer_provider or trace.get_tracer_provider()
        self._provider = tracer_provider
        self._tracer = provider.get_tracer(instrumentation_name)
        self._open: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def on_start(self, span: Span) -> None:
        with self._lock:
            parent = self._open.get(span.parent_id) if span.parent_id else None
        context = self._trace.set_span_in_context(parent) if parent is not None else None
        kind = self._trace.SpanKind.CLIENT if span.kind == "model" else self._trace.SpanKind.INTERNAL
        otel_span = self._tracer.start_span(span.name, context=context, kind=kind, start_time=_ns(span.start_time))
        with self._lock:
            self._open[span.span_id] = otel_span

    def export(self, span: Span) -> None:
        with self._lock:
            otel_span = self._open.pop(span.span_id, None)
        if otel_span is None:
            # Exporter added while the span was already open.
            self.on_start(span)
            with self._lock:
                otel_span = self._open.pop(span.span_id)
        otel_span.set_attribute("xuloom.span.kind", span.kind)
        otel_span.set_attribute("xuloom.trace_id", span.trace_id)
        for key, value in span.attributes.items():
            if value is not None:
                otel_span.set_attribute(key, _attribute(value))
        if span.status == "error":
            from opentelemetry.trace import Status, StatusCode
            otel_span.set_status(Status(StatusCode.ERROR, span.error or ""))
        otel_span.end(end_time=_ns(span.end_time or span.start_time))

    def shutdown(self) -> None:
        provider: Optional[Any] = self._provider
        if provider is not None and callable(getattr(provider, "force_flush", None)):
            provider.force_flush()


def otlp_exporter() -> OpenTelemetryTraceExporter:
    """An exporter with its own SDK provider sending OTLP/HTTP to ``OTEL_EXPORTER_OTLP_ENDPOINT``.

    Used by ``XULOOM_TRACE_OTLP=1`` so the CLI can export without host code.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider(resource=Resource.create({"service.name": "xuloom"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return OpenTelemetryTraceExporter(provider)
