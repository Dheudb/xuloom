import unittest
import asyncio
import os
import tempfile
from unittest.mock import patch
from xuloom.observability.cost import CostMeter, scoped_model


class ConnectionPricingTests(unittest.TestCase):
    def test_same_model_different_connections_and_shared_budget(self):
        meter = CostMeter({'same': {'input': 1, 'output': 2},
                           scoped_model('a', 'same'): {'input': 3, 'output': 4},
                           scoped_model('b', 'same'): {'input': 10, 'output': 20}}, limit_usd=12)
        self.assertEqual(meter.record('same', {'input_tokens': 1000000}, connection='a'), 3)
        self.assertEqual(meter.record('same', {'input_tokens': 1000000}, connection='b'), 10)
        self.assertIsNotNone(meter.blocked_reason('same', connection='a'))
        self.assertEqual(meter.price_for('same', connection='unknown').input, 1)

    def test_connection_pattern_does_not_match_other_connections(self):
        meter = CostMeter({scoped_model('a', 'model-*'): {'input': 0, 'output': 0}})
        self.assertIsNotNone(meter.price_for('model-x', connection='a'))
        self.assertIsNone(meter.price_for('model-x', connection='b'))
        self.assertIsNone(meter.price_for('model-x'))

    def test_engine_route_switch_clears_old_connection_price_identity(self):
        from xuloom import QueryEngine, EngineConfig
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'XULOOM_DATA_DIR':folder, 'XULOOM_CONFIG_DIR':folder}), \
                patch('xuloom.core.config.load_config', return_value={'model_connections':[]}):
            engine = QueryEngine(EngineConfig(cwd=folder, model_provider='openai', model='same', api_key='fixture',
                api_base_url='http://127.0.0.1:9/v1', network_mode='direct', bundled_plugins=False, model_connection_id='old'))
            try:
                self.assertEqual(engine.tool_context['pricing_connection_id'], 'old')
                engine.switch_model_runtime(api_base_url='http://127.0.0.1:10/v1', persist=False)
                self.assertIsNone(engine.tool_context['pricing_connection_id'])
            finally:
                asyncio.run(engine.aclose())
