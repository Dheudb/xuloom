import json
import os
import unittest
from unittest.mock import patch

from xuloom import EngineConfig, ModelPrice, PluginAPI
from xuloom.models import ToolCall, model_response
from xuloom.observability import CostMeter, JsonlTraceExporter, Tracer, load_prices

from tests.test_model_adapters import AdapterFixture


class Collect:
    def __init__(self):
        self.started, self.spans = [], []

    def on_start(self, span):
        self.started.append(span.name)

    def export(self, span):
        self.spans.append(span)

    def by_kind(self, kind):
        return [span for span in self.spans if span.kind == kind]


class Broken:
    def export(self, span):
        raise RuntimeError("backend down")


async def drain(engine, text):
    events = []
    async for event in engine.submit_message(text, sys_print_callback=lambda _: None):
        events.append(event)
    return events


class TracingTests(AdapterFixture):
    async def test_run_model_and_tool_spans_nest_with_usage_and_cost(self):
        self.adapter.script = [
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "hi\n"})],
                           input_tokens=1000, output_tokens=200),
            model_response(text="written", input_tokens=2000, output_tokens=100, cache_read_input_tokens=500),
        ]
        collect = Collect()
        prices = {"m": {"input": 1.0, "output": 10.0, "cache_read": 0.1}}
        async with self.engine(trace_exporters=(collect,), model_prices=prices) as engine:
            events = await drain(engine, "write a.txt")
            self.assertTrue(events[-1]["result"]["ok"], events[-1])
        run, = collect.by_kind("run")
        models, tools = collect.by_kind("model"), collect.by_kind("tool")
        self.assertEqual(len(models), 2)
        self.assertEqual([span.attributes["gen_ai.tool.name"] for span in tools], ["file_write_tool"])
        for span in models + tools:
            self.assertEqual((span.trace_id, span.parent_id), (run.trace_id, run.span_id))
            self.assertLessEqual(run.start_time, span.start_time)
            self.assertLessEqual(span.end_time, run.end_time)
        self.assertEqual(models[0].attributes["gen_ai.usage.input_tokens"], 1000)
        self.assertEqual(models[1].attributes["gen_ai.usage.cache_read_input_tokens"], 500)
        self.assertAlmostEqual(models[0].attributes["xuloom.cost_usd"], 0.003)
        self.assertAlmostEqual(models[1].attributes["xuloom.cost_usd"], 0.00305)
        self.assertAlmostEqual(run.attributes["xuloom.task.cost_usd"], 0.00605)
        self.assertEqual(run.attributes["xuloom.result.reason"], "completed")
        self.assertEqual(run.status, "ok")
        # Children are exported before their parent, and on_start saw every span.
        self.assertIs(collect.spans[-1], run)
        self.assertEqual(sorted(collect.started), sorted(span.name for span in collect.spans))
        usage = [event for event in events if event["type"] == "token_usage"]
        self.assertAlmostEqual(usage[-1]["session_cost_usd"], 0.00605)
        self.assertAlmostEqual(engine.session_cost["usd"], 0.00605)

    async def test_failing_exporter_never_breaks_the_run(self):
        self.adapter.script = [model_response(text="fine")]
        collect = Collect()
        with self.assertLogs("xuloom.observability", "WARNING") as logs:
            async with self.engine(trace_exporters=(Broken(), collect)) as engine:
                events = await drain(engine, "hi")
        self.assertTrue(events[-1]["result"]["ok"])
        self.assertEqual({span.kind for span in collect.spans}, {"run", "model"})
        self.assertEqual(len(logs.output), 1)  # warned once, not per span

    async def test_model_error_marks_spans_as_errors(self):
        self.adapter.script = [model_response(text="unused")]
        collect = Collect()
        async with self.engine(trace_exporters=(collect,)) as engine:
            engine.vcr_manager.try_replay = lambda payload: (_ for _ in ()).throw(ValueError("broken replay"))
            events = await drain(engine, "hi")
        self.assertFalse(events[-1]["result"]["ok"])
        model, = collect.by_kind("model")
        self.assertEqual((model.status, model.error), ("error", "ValueError: broken replay"))
        run, = collect.by_kind("run")
        self.assertEqual(run.status, "error")

    async def test_environment_trace_file_writes_jsonl(self):
        path = self.root / "trace" / "spans.jsonl"
        self.adapter.script = [model_response(text="ok", input_tokens=3, output_tokens=1)]
        with patch.dict(os.environ, {"XULOOM_TRACE_FILE": str(path)}):
            async with self.engine() as engine:
                await drain(engine, "hi")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["kind"] for row in rows], ["model", "run"])
        self.assertEqual(rows[0]["attributes"]["gen_ai.usage.output_tokens"], 1)
        self.assertEqual(rows[0]["parent_id"], rows[1]["span_id"])
        self.assertIsNotNone(rows[1]["duration_ms"])

    async def test_plugin_trace_exporter(self):
        collect = Collect()

        def register(api: PluginAPI):
            api.add_trace_exporter(collect)
        self.adapter.script = [model_response(text="ok")]
        async with self.engine(plugins=(register,)) as engine:
            await drain(engine, "hi")
        self.assertEqual({span.kind for span in collect.spans}, {"run", "model"})
        with self.assertRaises(TypeError):
            PluginAPI("x").add_trace_exporter(object())

    async def test_subagent_spans_and_cost_join_the_parent_task(self):
        self.adapter.script = [
            model_response(tool_calls=[ToolCall("a1", "agent_tool", {"prompt": "say child", "description": "child"})],
                           input_tokens=100, output_tokens=10),
        ] + [model_response(text="done", input_tokens=100, output_tokens=10) for _ in range(8)]
        collect = Collect()
        async with self.engine(trace_exporters=(collect,), model_prices={"m": {"input": 10, "output": 100}},
                               collaboration_mode="auto") as engine:
            events = await drain(engine, "delegate")
            self.assertTrue(events[-1]["result"]["ok"], events[-1])
            meter = engine.cost_meter
        runs = collect.by_kind("run")
        self.assertEqual(len(runs), 2)
        child, parent = sorted(runs, key=lambda span: span.attributes["xuloom.agent_depth"], reverse=True)
        tool, = [span for span in collect.by_kind("tool") if span.attributes["gen_ai.tool.name"] == "agent_tool"]
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertEqual(child.parent_id, tool.span_id)
        self.assertEqual(tool.parent_id, parent.span_id)
        # Every priced request (parent and child) adds to one task total: (100*10 + 10*100) / 1e6 each.
        requests = len(self.adapter.requests)
        self.assertGreaterEqual(requests, 3)
        self.assertEqual(len(collect.by_kind("model")), requests)
        self.assertAlmostEqual(meter.task_usd, 0.002 * requests)
        self.assertAlmostEqual(parent.attributes["xuloom.task.cost_usd"], 0.002 * requests)


class CostLimitTests(AdapterFixture):
    async def test_task_stops_before_the_request_that_would_start_over_the_cap(self):
        self.adapter.script = [
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "x"})],
                           input_tokens=1000, output_tokens=0),
            model_response(text="never requested"),
        ]
        async with self.engine(model_prices={"m": {"input": 2000, "output": 8}}, max_cost_usd=1.5) as engine:
            events = await drain(engine, "write")
            result = events[-1]["result"]
            self.assertEqual(result["reason"], "cost_limit")
            self.assertIn("$2.0000 spent of $1.5000", result["text"])
            self.assertEqual(len(self.adapter.requests), 1)
            # A new task starts with a fresh task total (session total keeps counting).
            self.adapter.script = [model_response(text="again", input_tokens=10)]
            events = await drain(engine, "next")
            self.assertTrue(events[-1]["result"]["ok"], events[-1])
            self.assertAlmostEqual(engine.session_cost["usd"], 2.02)

    async def test_cap_without_a_price_fails_closed_before_any_request(self):
        self.adapter.script = [model_response(text="never")]
        async with self.engine(max_cost_usd=10) as engine:
            events = await drain(engine, "hi")
        self.assertEqual(events[-1]["result"]["reason"], "cost_limit")
        self.assertIn("has no price", events[-1]["result"]["text"])
        self.assertEqual(self.adapter.requests, [])

    async def test_environment_cap_and_pricing_file(self):
        home = self.root / "home" / ".xuloom"
        home.mkdir(parents=True)
        (home / "pricing.json").write_text(json.dumps({"models": {"m*": {"input": 1, "output": 1}}}), encoding="utf-8")
        self.adapter.script = [model_response(text="ok", input_tokens=10)]
        with patch.dict(os.environ, {"XULOOM_MAX_COST_USD": "0.5"}):
            async with self.engine() as engine:
                self.assertEqual(engine.config.max_cost_usd, 0.5)
                events = await drain(engine, "hi")
        self.assertTrue(events[-1]["result"]["ok"], events[-1])

    async def test_session_cost_survives_reload(self):
        self.adapter.script = [model_response(text="ok", input_tokens=1000)]
        async with self.engine(model_prices={"m": {"input": 3000, "output": 0}}) as engine:
            await drain(engine, "hi")
            session_id = engine.session_id
        async with self.engine() as engine:
            engine.load_session(session_id)
            self.assertAlmostEqual(engine.session_cost["usd"], 3.0)


class PriceTests(unittest.TestCase):
    def test_price_math_and_validation(self):
        price = ModelPrice.coerce({"input": 3, "output": 15, "cache_write": 3.75})
        usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_creation_input_tokens": 1_000_000,
                 "cache_read_input_tokens": 1_000_000}
        # cache_read defaults to the input price.
        self.assertAlmostEqual(price.cost(usage), 3 + 1.5 + 3.75 + 3)
        for bad in ({"input": -1, "output": 1}, {"input": 1, "output": float("nan")}, {"input": 1, "output": 1, "x": 1}):
            with self.assertRaises(ValueError):
                ModelPrice.coerce(bad)
        with self.assertRaises(ValueError):
            EngineConfig(max_cost_usd=-1)
        with self.assertRaises(ValueError):
            EngineConfig().resolved({"XULOOM_MAX_COST_USD": "lots"})

    def test_exact_model_beats_pattern(self):
        meter = CostMeter({"gpt-*": {"input": 1, "output": 1}, "gpt-x": {"input": 5, "output": 5}})
        self.assertEqual(meter.price_for("gpt-x").input, 5)
        self.assertEqual(meter.price_for("gpt-y").input, 1)
        self.assertIsNone(meter.price_for("claude"))

    def test_pricing_file_errors_are_reported(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pricing.json"
            self.assertEqual(load_prices(path), {})
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_prices(path)
            path.write_text(json.dumps({"models": {"m": {"input": "x", "output": 1}}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_prices(path)

    def test_public_config_hides_live_objects(self):
        data = EngineConfig(trace_exporters=(JsonlTraceExporter("x.jsonl"),), model_prices={"m": {"input": 1, "output": 1}},
                            max_cost_usd=2).to_public_dict()
        self.assertEqual(data["trace_exporters"], ["JsonlTraceExporter"])
        self.assertEqual(data["model_prices"], ["m"])
        self.assertEqual(data["max_cost_usd"], 2.0)


class OpenTelemetryTests(unittest.TestCase):
    def test_spans_are_mirrored_with_parents_and_errors(self):
        import sys
        import types
        recorded = []

        class FakeSpan:
            def __init__(self, name, context, kind, start_time):
                self.name, self.parent, self.start, self.attributes, self.status, self.end_time = name, context, start_time, {}, None, None
                recorded.append(self)

            def set_attribute(self, key, value):
                self.attributes[key] = value

            def set_status(self, status):
                self.status = status

            def end(self, end_time):
                self.end_time = end_time

        class FakeTracer:
            def start_span(self, name, context=None, kind=None, start_time=None):
                return FakeSpan(name, context, kind, start_time)

        trace = types.ModuleType("opentelemetry.trace")
        trace.SpanKind = types.SimpleNamespace(CLIENT="client", INTERNAL="internal")
        trace.set_span_in_context = lambda span: ("ctx", span)
        trace.get_tracer_provider = lambda: types.SimpleNamespace(get_tracer=lambda name: FakeTracer())
        trace.Status = lambda code, message: (code, message)
        trace.StatusCode = types.SimpleNamespace(ERROR="ERROR")
        package = types.ModuleType("opentelemetry")
        package.trace = trace
        with patch.dict(sys.modules, {"opentelemetry": package, "opentelemetry.trace": trace}):
            from xuloom.observability.otel import OpenTelemetryTraceExporter
            tracer = Tracer([OpenTelemetryTraceExporter()])
            with tracer.span("xuloom.run", "run", **{"xuloom.agent_depth": 0}):
                try:
                    with tracer.span("xuloom.model", "model", **{"gen_ai.request.model": "m",
                                                                  "gen_ai.response.finish_reasons": ["end_turn"]}):
                        raise RuntimeError("boom")
                except RuntimeError:
                    pass
        run, model = recorded
        self.assertEqual(model.parent, ("ctx", run))
        self.assertIsNone(run.parent)
        self.assertEqual(model.attributes["gen_ai.request.model"], "m")
        self.assertEqual(model.attributes["gen_ai.response.finish_reasons"], ["end_turn"])
        self.assertEqual(model.status, ("ERROR", "RuntimeError: boom"))
        self.assertIsNone(run.status)
        self.assertGreaterEqual(run.end_time, model.end_time)


if __name__ == "__main__":
    unittest.main()
