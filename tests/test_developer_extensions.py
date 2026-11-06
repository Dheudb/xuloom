import unittest
from tests import test_tool_recovery as recovery
from examples.extension_project.app import EvidenceTool


class ExtensionContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=recovery.RecoveryTests.asyncSetUp
    asyncTearDown=recovery.RecoveryTests.asyncTearDown
    fixture=recovery.RecoveryTests.fixture

    async def test_registration_cannot_replace_builtin_or_existing_tool_and_requires_idle(self):
        self.engine.register_tool(EvidenceTool())
        with self.assertRaises(ValueError):self.engine.register_tool(EvidenceTool())
        bad=EvidenceTool();bad.name='file_write_tool'
        with self.assertRaises(ValueError):self.engine.register_tool(bad)
        self.fixture(['DONE']);stream=self.engine.submit_message('Task',sys_print_callback=lambda _:None)
        await stream.__anext__()
        try:
            with self.assertRaises(ValueError):self.engine.register_tool(EvidenceTool())
        finally:await stream.aclose()

    async def test_listener_failure_does_not_change_tool_execution_or_receipt(self):
        self.engine.register_tool(EvidenceTool())
        def listener(event):raise RuntimeError('PRIVATE-LISTENER-PAYLOAD')
        self.engine.set_event_handler(listener)
        self.fixture([('evidence_tool','{"value":"ONCE"}'),'DONE'])
        with self.assertLogs('xuloom.core.engine',level='WARNING') as logs:
            result=await self.engine.run('Record evidence',sys_print_callback=lambda _:None)
        self.assertTrue(result.ok,result)
        self.assertNotIn('PRIVATE-LISTENER-PAYLOAD','\n'.join(logs.output))
        rows=[row for row in self.engine.recent_tool_activity if row['tool_name']=='evidence_tool']
        self.assertEqual(len(rows),1);self.assertFalse(rows[0]['tool_result']['is_error'])

    async def test_async_listener_rejected_and_sync_listener_cannot_mutate_tool_input(self):
        async def async_listener(event):pass
        with self.assertRaises(TypeError):self.engine.set_event_handler(async_listener)
        def listener(event):
            if event['type']=='tool_started':event['input']['value']='MUTATED'
        self.engine.set_event_handler(listener)
        self.fixture([('record_tool','{"value":"ORIGINAL"}'),'DONE'])
        await self.engine.run('Record evidence',sys_print_callback=lambda _:None)
        self.assertEqual(self.tool.calls,['ORIGINAL'])
