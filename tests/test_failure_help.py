import unittest
from unittest.mock import AsyncMock
import httpx
from openai import AuthenticationError, BadRequestError
from tests import test_tool_recovery as recovery
from xuloom.core.run_result import failure_help
from xuloom.core.network import model_error_message


class FailureHelpTests(unittest.TestCase):
    def test_existing_reasons_have_distinct_actions_without_new_execution_states(self):
        self.assertEqual(failure_help('not_configured')['action'],'settings')
        self.assertEqual(failure_help('tool_input_error')['category'],'tool_input')
        self.assertEqual(failure_help('approval_required')['category'],'permission')
        self.assertEqual(failure_help('blocked')['category'],'blocked')
        self.assertIn('可能已生效',failure_help('persistence_error')['message'])

    def test_documented_thinking_parameter_errors_are_actionable_without_echoing_body(self):
        for detail, field in [("Thinking mode does not support this tool_choice", "tool_choice"),
                              ("The reasoning_content in the thinking mode must be passed back to the API.", "reasoning_content")]:
            error = BadRequestError(detail + " PRIVATE-KEY", response=httpx.Response(400,
                request=httpx.Request('POST', 'https://private.invalid')), body={'error': {'message': detail, 'private': 'SECRET'}})
            message = model_error_message(error)
            self.assertIn(field, message)
            self.assertIn('400', message)
            for private in ['PRIVATE-KEY', 'SECRET', 'private.invalid']:
                self.assertNotIn(private, message)

    def test_provider_error_text_omits_untrusted_response_bodies(self):
        error=AuthenticationError('PRIVATE-RESPONSE-SECRET',response=httpx.Response(401,request=httpx.Request('POST','https://fixture.invalid')),body={'private':'SECRET'})
        message=model_error_message(error)
        self.assertIn('401',message);self.assertIn('Key',message)
        self.assertNotIn('SECRET',message);self.assertNotIn('fixture.invalid',message)
        self.assertIn('依赖',model_error_message(ImportError('PRIVATE-PATH')))


class FailureWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=recovery.RecoveryTests.asyncSetUp
    asyncTearDown=recovery.RecoveryTests.asyncTearDown
    fixture=recovery.RecoveryTests.fixture

    async def test_sdk_authentication_failure_retains_history_and_returns_actionable_text(self):
        self.engine.client.chat.completions.create=AsyncMock(side_effect=AuthenticationError('PRIVATE-KEY',
            response=httpx.Response(401,request=httpx.Request('POST','http://127.0.0.1/v1')),body={'message':'PRIVATE-KEY'}))
        result=await self.engine.run('Keep original task',sys_print_callback=lambda _:None)
        self.assertFalse(result.ok);self.assertEqual(result.reason,'api_error')
        self.assertIn('401',result.text);self.assertNotIn('PRIVATE-KEY',result.text)
        self.assertTrue(result.persisted)

    async def test_auth_failure_body_cannot_trigger_transient_fallback(self):
        error=AuthenticationError('rate limit overloaded',response=httpx.Response(401,request=httpx.Request('POST','http://127.0.0.1/v1')),body={})
        self.assertFalse(self.engine._is_model_fallback_error(error))
