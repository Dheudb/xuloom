"""Native currency accounting; all requests use the local scripted adapter."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_model_adapters import AdapterFixture
from xuloom.desktop.turns import TurnRecorder
from xuloom.models import model_response
from xuloom.observability.cost import CostMeter, ModelPrice, load_pricing_settings, save_pricing_settings


class CurrencyTests(unittest.TestCase):
    def test_native_price_round_trip_and_cache_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'pricing.json'
            save_pricing_settings({'carbon': {'input': 28.4, 'output': 142, 'currency': 'CNY'}}, None, path,
                                  max_cost_cny=5)
            price = load_pricing_settings(path)[0]['carbon']
            self.assertEqual(price.currency, 'CNY')
            self.assertAlmostEqual(price.cost({'input_tokens': 500000, 'cache_read_input_tokens': 100000,
                                               'output_tokens': 200000}), 45.44)
            # Legacy save callers preserve the independent RMB cap.
            save_pricing_settings({'carbon': price}, 1, path)
            self.assertEqual(json.loads(path.read_text())['max_cost_cny'], 5)
            for currency in ('RMB', '', 'EUR'):
                with self.assertRaises(ValueError):
                    ModelPrice.coerce({'input': 1, 'output': 1, 'currency': currency})

    def test_mixed_meter_never_adds_yuan_to_dollars_and_checks_both_caps(self):
        prices = {'cn': {'input': 1, 'output': 0, 'currency': 'CNY'}, 'us': {'input': 2, 'output': 0}}
        meter = CostMeter(prices, limit_usd=5, limit_cny=3)
        meter.record('cn', {'input_tokens': 1000000})
        meter.record('us', {'input_tokens': 1000000})
        self.assertEqual(meter.task_amounts, {'CNY': 1, 'USD': 2})
        self.assertEqual(meter.task_usd, 2)
        self.assertIsNone(meter.blocked_reason('cn'))
        meter.record('cn', {'input_tokens': 2000000})
        self.assertIn('CNY', meter.blocked_reason('us'))
        meter.start_task()
        self.assertEqual(meter.task_amounts, {})
        self.assertEqual(meter.task_usd, 0)
        usd_only = CostMeter(prices, limit_usd=5)
        self.assertIn('CNY', usd_only.blocked_reason('cn'))

    def test_desktop_does_not_classify_rmb_as_unpriced(self):
        recorder = TurnRecorder()
        recorder.accept({'type': 'token_usage', 'input_tokens': 10, 'output_tokens': 2,
                         'cost_amount': 0.02, 'cost_currency': 'CNY', 'cost_usd': None,
                         'task_cost_usd': 0, 'task_costs': {'CNY': 0.02}, 'usage_source': 'api_usage'})
        usage = recorder.record['usage']
        self.assertEqual(usage['unpricedRequests'], 0)
        self.assertEqual(usage['costAmounts'], {'CNY': 0.02})


class CurrencyEngineTests(AdapterFixture):
    async def test_rmb_cap_survives_restart_and_stops_without_a_second_request(self):
        from xuloom.models import ToolCall
        self.adapter.script = [model_response(tool_calls=[ToolCall('c', 'file_write_tool',
                               {'file_path': 'result.txt', 'content': 'kept'})], input_tokens=2000)]
        settings = dict(model_prices={'m': {'input': 1000, 'output': 0, 'currency': 'CNY'}}, max_cost_cny=1)
        async with self.engine(**settings) as engine:
            result = await engine.run('write', sys_print_callback=lambda _: None)
            self.assertEqual(result.reason, 'cost_limit')
            session = engine.session_id
        count = len(self.adapter.requests)
        async with self.engine(**settings) as engine:
            self.assertTrue(engine.load_session(session))
            self.assertEqual(engine.cost_meter.task_amounts, {'CNY': 2})
            events = [e async for e in engine.submit_message('', resume=True, sys_print_callback=lambda _: None)]
            self.assertEqual(events[-1]['result']['reason'], 'cost_limit')
            self.assertEqual(len(self.adapter.requests), count)
            self.assertEqual(engine.session_cost['amounts'], {'CNY': 2})

    async def test_cny_file_cap_environment_precedence_and_rpc_reload(self):
        import os
        from xuloom.desktop.bridge import DesktopBridge
        home = self.root / 'home' / '.xuloom'
        home.mkdir(parents=True, exist_ok=True)
        (home / 'pricing.json').write_text(json.dumps({'models': {
            'm': {'input': 28.4, 'output': 142, 'currency': 'CNY'}}, 'max_cost_cny': 5}))
        async with self.engine() as engine:
            self.assertEqual(engine.cost_meter.limit_cny, 5)
            bridge = DesktopBridge(lambda _: None, cwd=str(self.root / 'work'))
            bridge.engine = engine
            data = await bridge.dispatch('pricing.save', {'models': {'m': {
                'input': 21.3, 'output': 106.5, 'currency': 'CNY'}}, 'maxCostCny': 8})
            self.assertEqual(data['maxCostCny'], 8)
            self.assertEqual(data['models']['m']['currency'], 'CNY')
            self.assertEqual(engine.cost_meter.limit_cny, 8)
            self.assertEqual(engine.cost_meter.prices['m'].input, 21.3)
        with patch.dict(os.environ, {'XULOOM_MAX_COST_CNY': '2'}):
            async with self.engine() as engine:
                self.assertEqual(engine.cost_meter.limit_cny, 2)
            async with self.engine(max_cost_cny=3) as engine:
                self.assertEqual(engine.cost_meter.limit_cny, 3)

    async def test_yuan_events_compaction_persistence_and_status(self):
        self.adapter.script = [model_response(text='ok', input_tokens=1000, output_tokens=100)]
        async with self.engine(model_prices={'m': {'input': 28.4, 'output': 142, 'currency': 'CNY'}},
                               max_cost_cny=10) as engine:
            events = [e async for e in engine.submit_message('hi', sys_print_callback=lambda _: None)]
            usage = next(e for e in events if e['type'] == 'token_usage')
            self.assertEqual(usage['cost_currency'], 'CNY')
            self.assertAlmostEqual(usage['cost_amount'], 0.0426)
            self.assertIsNone(usage['cost_usd'])
            self.assertEqual(engine.session_cost['usd'], 0)
            self.assertIn('¥', engine.get_runtime_summary())
            engine._record_auxiliary_usage('compaction', {'model': 'm',
                                            'input_tokens': 1000, 'output_tokens': 100})
            self.assertAlmostEqual(engine.cost_meter.task_amounts['CNY'], 0.0852)
            self.assertEqual(engine.auxiliary_usage['compaction'].get('cost_usd', 0), 0)
            metadata = engine._build_session_metadata()
            self.assertAlmostEqual(metadata['task_cost']['amounts']['CNY'], 0.0852)
            self.assertAlmostEqual(metadata['session_cost']['amounts']['CNY'], 0.0852)


if __name__ == '__main__':
    unittest.main()
