"""Cost settings shared by the desktop app and CLI (~/.xuloom/pricing.json) and per-turn usage for the UI."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.desktop.turns import TurnRecorder
from xuloom.models import model_response
from xuloom.observability.cost import load_pricing_settings, save_pricing_settings

from tests.test_model_adapters import AdapterFixture


class PricingFileTests(unittest.TestCase):
    def test_round_trip_with_cap_and_free_local_models(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pricing.json"
            save_pricing_settings({"gpt-4.1": {"input": 2, "output": 8}, "llama*": {"input": 0, "output": 0}}, 0.5, path)
            prices, cap = load_pricing_settings(path)
            self.assertEqual((prices["gpt-4.1"].output, prices["llama*"].input, cap), (8.0, 0.0, 0.5))
            save_pricing_settings({}, None, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"models": {}})

    def test_invalid_settings_are_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pricing.json"
            for models, cap in (({"m": {"input": -1, "output": 1}}, None), ({" ": {"input": 1, "output": 1}}, None),
                                ({}, -2), ({"m": {"input": 1}}, None)):
                with self.subTest(models=models, cap=cap), self.assertRaises(ValueError):
                    save_pricing_settings(models, cap, path)
            self.assertFalse(path.exists())
            path.write_text(json.dumps({"models": {}, "max_cost_usd": "lots"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_pricing_settings(path)


class EngineCapTests(AdapterFixture):
    def write_pricing(self, payload):
        home = self.root / "home" / ".xuloom"
        home.mkdir(parents=True, exist_ok=True)
        (home / "pricing.json").write_text(json.dumps(payload), encoding="utf-8")

    async def test_cap_in_pricing_file_applies_and_explicit_config_wins(self):
        self.write_pricing({"models": {"m": {"input": 0, "output": 0}}, "max_cost_usd": 1.5})
        async with self.engine() as engine:
            self.assertEqual(engine.cost_meter.limit_usd, 1.5)
        async with self.engine(max_cost_usd=0.25) as engine:
            self.assertEqual(engine.cost_meter.limit_usd, 0.25)

    async def test_free_local_model_runs_under_a_cap(self):
        self.write_pricing({"models": {"m": {"input": 0, "output": 0}}, "max_cost_usd": 0.01})
        self.adapter.script = [model_response(text="local answer", input_tokens=5000, output_tokens=500)]
        async with self.engine() as engine:
            result = await engine.run("hi", sys_print_callback=lambda *_: None)
        self.assertTrue(result.ok, result)

    async def test_context_status_reports_session_cost(self):
        from xuloom.context.compaction import status
        self.write_pricing({"models": {"m": {"input": 1000, "output": 0}}})
        self.adapter.script = [model_response(text="x", input_tokens=10)]
        async with self.engine() as engine:
            await engine.run("hi", sys_print_callback=lambda *_: None)
            cost = status(engine)["cost"]
        self.assertAlmostEqual(cost["usd"], 0.01)
        self.assertIsNone(cost["limitUsd"])


class TurnUsageTests(unittest.TestCase):
    def test_usage_accumulates_and_flags_unpriced_and_estimated(self):
        recorder = TurnRecorder()
        recorder.accept({"type": "token_usage", "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.002,
                         "task_cost_usd": 0.002, "usage_source": "api_usage"})
        self.assertEqual(recorder.record["usage"], {"inputTokens": 100, "outputTokens": 20, "requests": 1,
                                                    "unpricedRequests": 0, "estimated": False, "costUsd": 0.002})
        recorder.accept({"type": "token_usage", "input_tokens": 50, "output_tokens": 5, "cost_usd": None,
                         "task_cost_usd": 0.002, "usage_source": "heuristic"})
        usage = recorder.record["usage"]
        self.assertEqual((usage["inputTokens"], usage["requests"], usage["unpricedRequests"], usage["estimated"]),
                         (150, 2, 1, True))
        self.assertFalse(recorder.accept({"type": "token_usage", "agent_id": "child", "input_tokens": 9}))
        self.assertEqual(recorder.record["usage"]["inputTokens"], 150)


class BridgePricingTests(unittest.TestCase):
    def test_pricing_rpc_saves_and_updates_the_open_engine(self):
        from xuloom.desktop.bridge import DesktopBridge
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "HOME": folder, "USERPROFILE": folder, "XULOOM_DATA_DIR": folder + "/data"}):
            os.environ.pop("XULOOM_MAX_COST_USD", None)
            async def run():
                bridge = DesktopBridge(lambda event: None, cwd=folder)
                class Meter:
                    prices, limit_usd = {}, None
                class Config:
                    model_prices, max_cost_usd = None, None
                bridge.engine = type("E", (), {"cost_meter": Meter(), "config": Config()})()
                empty = await bridge.dispatch("pricing.get", {})
                saved = await bridge.dispatch("pricing.save", {"models": {"local-*": {"input": 0, "output": 0}}, "maxCostUsd": 2})
                return empty, saved, bridge.engine.cost_meter
            empty, saved, meter = asyncio.run(run())
            self.assertEqual((empty["models"], empty["maxCostUsd"]), ({}, None))
            self.assertEqual(saved["models"], {"local-*": {"input": 0.0, "output": 0.0}})
            self.assertEqual(saved["maxCostUsd"], 2.0)
            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual((meter.limit_usd, sorted(meter.prices)), (2.0, ["local-*"]))


if __name__ == "__main__":
    unittest.main()
