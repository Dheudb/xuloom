import unittest
from tests import test_tool_recovery as recovery


class ConsumerCancellationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    async def test_closing_event_consumer_persists_cancellation_and_keeps_original_task(self):
        self.fixture(['DONE'])
        stream = self.engine.submit_message('Original task', sys_print_callback=lambda _: None)
        await stream.__anext__()
        await stream.aclose()
        self.assertEqual(self.engine.last_run_result['reason'], 'cancelled')
        self.assertTrue(self.engine.last_run_result['persisted'])
        self.assertFalse(self.engine.has_active_turn())
        saved = self.engine.session_manager.load_session_state(self.engine.session_id)
        self.assertEqual(saved['metadata']['last_run_result']['reason'], 'cancelled')
