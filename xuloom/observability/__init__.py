"""Tracing and cost accounting for engine runs.

* :class:`Tracer` / :class:`Span`: run, model-request and tool-call spans sent to
  exporters (:class:`JsonlTraceExporter`, :class:`OpenTelemetryTraceExporter`
  or any object with ``export(span)``).
* :class:`CostMeter` / :class:`ModelPrice`: request cost from per-model prices and
  the per-task ``max_cost_usd`` cap.

Configure with ``EngineConfig(trace_exporters=..., model_prices=..., max_cost_usd=...)``,
the environment (``XULOOM_TRACE_FILE``, ``XULOOM_TRACE_OTLP``, ``XULOOM_MAX_COST_USD``)
or a plugin (``api.add_trace_exporter``). See ``docs/observability.md``.
"""
from typing import Any

from xuloom.observability.cost import CostLimitError, CostMeter, ModelPrice, load_prices, pricing_path
from xuloom.observability.tracing import JsonlTraceExporter, Span, TraceExporter, Tracer, current_span

__all__ = [
    "CostLimitError", "CostMeter", "ModelPrice", "load_prices", "pricing_path",
    "JsonlTraceExporter", "Span", "TraceExporter", "Tracer", "current_span",
    "OpenTelemetryTraceExporter", "environment_exporters",
]


def __getattr__(name):
    if name == "OpenTelemetryTraceExporter":
        from xuloom.observability.otel import OpenTelemetryTraceExporter
        return OpenTelemetryTraceExporter
    raise AttributeError(name)


def environment_exporters(environ=None) -> list:
    """Exporters requested by ``XULOOM_TRACE_FILE`` (JSONL path) and ``XULOOM_TRACE_OTLP=1``."""
    import os
    env = os.environ if environ is None else environ
    exporters = []
    path = str(env.get("XULOOM_TRACE_FILE", "") or "").strip()
    if path:
        exporters.append(JsonlTraceExporter(path))
    if str(env.get("XULOOM_TRACE_OTLP", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
        global _OTLP
        if _OTLP is None:
            try:
                from xuloom.observability.otel import otlp_exporter
                _OTLP = otlp_exporter()  # one provider per process, shared by every engine
            except ImportError as error:
                import logging
                logging.getLogger("xuloom.observability").warning(
                    "XULOOM_TRACE_OTLP is set but OpenTelemetry is not installed (pip install xuloom[otel]): %s", error)
                _OTLP = False
        if _OTLP:
            exporters.append(_OTLP)
    return exporters


_OTLP: Any = None
