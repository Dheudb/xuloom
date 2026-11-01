"""Mandatory live parent decisions without a model request or remote credentials."""
import asyncio
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from xuloom.security.delegation import authorize_delegated, request_from_wire
from xuloom.security.permissions import ApprovalPolicy, PermissionDecision, PermissionManager, PermissionRequest
from tests import test_tool_recovery as recovery


class Tool:
    name = "file_write_tool"
    description = "Write a fixture"
    is_read_only = False


class AuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "permissions.json"
        self.mode = "normal"
        self.answers, self.requests = [], []
        async def prompt(request):
            self.requests.append(request)
            return self.answers.pop(0) if self.answers else "deny"
        self.manager = PermissionManager(prompt_handler=prompt, mode_getter=lambda: self.mode,
            config_path=str(self.path), approval_policy=ApprovalPolicy("full"))
        self.request = PermissionRequest("file_write_tool", "Write /remote/answer.txt", "high", {"file_path": "/remote/answer.txt"},
            "normal", False, "Write", {})

    def tearDown(self):
        self.temp.cleanup()

    async def test_full_readonly_and_explicit_allow_must_still_pass_the_external_gate(self):
        for readonly, mode, config in ((False, "full", {}), (True, "ask", {}), (False, "ask", {"alwaysAllow": [Tool.name]})):
            self.path.write_text(json.dumps(config), encoding="utf-8")
            self.manager.approval_policy.set_mode(mode)
            tool = Tool(); tool.is_read_only = readonly
            calls = []
            async def deny(request, automatic, aliases):
                calls.append(automatic)
                return PermissionDecision("deny", "Parent denied", source="fixture")
            self.manager.delegated_authorizer = deny
            self.assertEqual((await self.manager.authorize(tool, {})).behavior, "deny")
            self.assertEqual(len(calls), 1); self.assertEqual(calls[0].behavior, "allow")

    async def test_unavailable_malformed_and_cancelled_authority_never_authorize(self):
        async def lost(*args): raise ConnectionError("DO_NOT_EXPOSE_AUTHORITY_DETAILS")
        self.manager.delegated_authorizer = lost
        decision = await self.manager.authorize(Tool(), {})
        self.assertEqual(decision.behavior, "deny"); self.assertNotIn("DO_NOT_EXPOSE", decision.reason)
        self.manager.delegated_authorizer = lambda *args: "allow"
        self.assertEqual((await self.manager.authorize(Tool(), {})).behavior, "deny")
        self.manager.consecutive_denials.clear()
        waiting = asyncio.Event()
        async def hold(*args): waiting.set(); await asyncio.Future()
        self.manager.delegated_authorizer = hold
        task = asyncio.create_task(self.manager.authorize(Tool(), {})); await waiting.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_local_plan_or_explicit_denial_cannot_be_overridden_by_a_parent_allow(self):
        calls = []
        self.manager.delegated_authorizer = lambda *args: calls.append(args) or PermissionDecision("allow")
        self.mode = "plan"
        self.assertEqual((await self.manager.authorize(Tool(), {"file_path": "/remote/file"})).behavior, "deny")
        self.mode = "normal"; self.path.write_text(json.dumps({"alwaysDeny": [Tool.name]}), encoding="utf-8")
        self.assertEqual((await self.manager.authorize(Tool(), {})).behavior, "deny")
        self.assertEqual(calls, [])

    async def test_live_parent_denies_asks_and_mode_changes_override_remote_full(self):
        automatic = PermissionDecision("allow", source="full_access")
        self.assertEqual((await authorize_delegated(self.manager, self.request, automatic))["behavior"], "allow")
        self.path.write_text(json.dumps({"alwaysDeny": [Tool.name]}), encoding="utf-8")
        self.assertEqual((await authorize_delegated(self.manager, self.request, automatic))["behavior"], "deny")
        self.path.write_text(json.dumps({"alwaysAsk": [Tool.name]}), encoding="utf-8")
        self.answers.append("allow")
        value = await authorize_delegated(self.manager, self.request, automatic, remote={"host": "lab", "cwd": "/work"})
        self.assertEqual(value["behavior"], "allow"); self.assertEqual(len(self.requests), 1)
        self.assertIn("lab", self.requests[0].summary)
        self.assertTrue(self.requests[0].classifier_input["forced_prompt_reasons"])
        self.path.write_text("{}", encoding="utf-8"); self.manager.approval_policy.set_mode("ask")
        self.assertEqual((await authorize_delegated(self.manager, self.request, automatic))["behavior"], "deny")
        self.assertEqual(len(self.requests), 2)

    async def test_readonly_explicit_ask_alias_and_ancestry_are_rechecked(self):
        readonly = dataclasses.replace(self.request, tool_name="file_read_tool", is_read_only=True)
        self.path.write_text(json.dumps({"alwaysAsk": ["read_alias"]}), encoding="utf-8")
        self.answers.append("allow")
        result = await authorize_delegated(self.manager, readonly, aliases={"read_alias": {}})
        self.assertEqual(result["behavior"], "allow"); self.assertEqual(len(self.requests), 1)
        ancestor = PermissionManager(config_path=str(Path(self.temp.name) / "ancestor.json"))
        ancestor.always_deny_tools.add(readonly.tool_name); self.manager.restriction_parent = ancestor
        self.assertEqual((await authorize_delegated(self.manager, readonly))["behavior"], "deny")
        self.manager.restriction_parent = self.manager
        self.assertEqual((await authorize_delegated(self.manager, readonly))["behavior"], "deny")

    async def test_child_grants_do_not_inherit_parent_memory_and_expire_after_mode_change(self):
        self.manager.approval_policy.set_mode("ask"); self.manager.always_allow_tools.add(Tool.name)
        self.assertEqual((await authorize_delegated(self.manager, self.request))["behavior"], "deny")
        self.answers.append("always")
        value = await authorize_delegated(self.manager, self.request)
        self.assertEqual(value["remember"], "allow")
        automatic = PermissionDecision("allow", source="session_rule")
        self.assertEqual((await authorize_delegated(self.manager, self.request, automatic, grant_revision=value["approval_revision"]))["behavior"], "allow")
        self.manager.approval_policy.set_mode("full"); self.manager.approval_policy.set_mode("ask")
        self.assertEqual((await authorize_delegated(self.manager, self.request, automatic, grant_revision=value["approval_revision"]))["behavior"], "deny")

    async def test_remote_forced_approval_survives_full_access_and_bad_parent_config_fails_closed(self):
        self.request.classifier_input["forced_prompt_reasons"] = ["Previous submission outcome is unknown"]
        self.answers.append("allow")
        result = await authorize_delegated(self.manager, self.request)
        self.assertEqual(result["behavior"], "allow"); self.assertEqual(len(self.requests), 1)
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual((await authorize_delegated(self.manager, self.request))["behavior"], "deny")
        self.mode = "plan"; self.path.write_text("{}", encoding="utf-8")
        self.assertEqual((await authorize_delegated(self.manager, self.request))["behavior"], "deny")

    async def test_full_access_descendant_does_not_override_an_ask_mode_ancestor(self):
        ancestor = PermissionManager(config_path=str(self.path), approval_policy=ApprovalPolicy("ask"))
        self.manager.restriction_parent = ancestor
        result = await authorize_delegated(self.manager, self.request)
        self.assertEqual(result["behavior"], "deny"); self.assertEqual(len(self.requests), 1)
        self.assertIn("ancestor", self.requests[0].summary)

    def test_wire_request_is_strict_and_does_not_share_mutable_payloads(self):
        wire = dataclasses.asdict(self.request); result = request_from_wire(wire)
        result.input_payload["file_path"] = "changed"
        self.assertEqual(wire["input_payload"]["file_path"], "/remote/answer.txt")
        for patch in ({"is_read_only": "true"}, {"denial_count": True}, {"tool_name": "bad name"}, {"extra": 1}, {"classifier_input": {"forced_prompt_reasons": "allow"}}):
            with self.assertRaises(ValueError): request_from_wire({**wire, **patch})


class RealToolGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_owned_execution_cannot_use_an_ordinary_cli_or_direct_tool_without_its_controller(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        restored = None
        try:
            self.engine.tool_context["delegation"] = {"authority_required": True, "remote_worker": {"version": 1}}
            self.assertTrue(self.engine.persist_session_state())
            receipt = await self.engine._execute_tool_wrapper({"id": "missing-controller", "name": "record_tool", "input": {"value": "must-not-run"}}, lambda _: None)
            self.assertTrue(receipt["is_error"]); self.assertEqual(self.tool.calls, [])
            from xuloom.core.engine import QueryEngine
            self.engine.session_manager.release_writer()
            restored = QueryEngine(model="fixture", model_provider="openai", cwd=str(self.root))
            self.assertTrue(restored.load_session(self.engine.session_id))
            self.assertTrue(restored.tool_context["delegation"]["authority_required"])
            from unittest.mock import AsyncMock, patch
            with patch.object(restored.client.chat.completions, "create", new=AsyncMock(side_effect=AssertionError("A model request is not authorized"))) as request:
                result = await restored.run("Do not execute through an unrelated entry point", sys_print_callback=lambda _: None)
                self.assertEqual(result.reason, "delegation_unavailable"); request.assert_not_called()
            import uuid
            independent = str(uuid.uuid4())
            restored.session_manager.acquire_writer(independent)
            self.assertTrue(restored.session_manager.save_session_state(independent, [], {}))
            restored.session_manager.release_writer(independent)
            restored.permission_manager.delegated_authorizer = lambda *args: PermissionDecision("allow", "old controller")
            self.assertTrue(restored.load_session(independent))
            self.assertNotIn("delegation", restored.tool_context)
            self.assertIsNone(restored.permission_manager.delegated_authorizer)
        finally:
            if restored is not None: await restored.aclose()
            await recovery.RecoveryTests.asyncTearDown(self)

    async def test_a_denied_gate_never_executes_the_real_engine_tool(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        try:
            self.engine.permission_manager.approval_policy.set_mode("full")
            self.engine.permission_manager.delegated_authorizer = lambda *args: PermissionDecision("deny", "parent rule")
            receipt = await self.engine._execute_tool_wrapper({"id": "denied-gate", "name": "record_tool", "input": {"value": "must-not-run"}}, lambda _: None)
            self.assertTrue(receipt["is_error"]); self.assertEqual(self.tool.calls, [])
            self.engine.permission_manager.delegated_authorizer = lambda *args: PermissionDecision("allow", "parent approved")
            receipt = await self.engine._execute_tool_wrapper({"id": "approved-gate", "name": "record_tool", "input": {"value": "allowed"}}, lambda _: None)
            self.assertFalse(receipt["is_error"]); self.assertEqual(self.tool.calls, ["allowed"])
        finally:
            await recovery.RecoveryTests.asyncTearDown(self)
