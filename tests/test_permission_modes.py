import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xuloom.security.permissions import ApprovalPolicy, PermissionManager
from xuloom.features.plans import get_plan_file_path


class PermissionModeTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, handler=None, policy=None, mode="normal"):
        return PermissionManager(prompt_handler=handler, approval_policy=policy, mode_getter=lambda: mode,
                                 config_path="nonexistent-test-permissions.json", session_id_getter=lambda: "fixture")

    async def test_request_mode_prompts_for_writes_and_allows_reads(self):
        approve = AsyncMock(return_value="allow")
        manager = self.manager(approve)
        self.assertEqual((await manager.authorize(SimpleNamespace(name="write", is_read_only=False), {})).behavior, "allow")
        approve.assert_awaited_once()
        self.assertEqual((await manager.authorize(SimpleNamespace(name="read", is_read_only=True), {})).source, "read_only")
        approve.assert_awaited_once()

    async def test_full_access_preserves_explicit_denials(self):
        approve = AsyncMock(side_effect=AssertionError("No approval in full mode"))
        manager = self.manager(approve, ApprovalPolicy("full"))
        manager.always_deny_tools.add("write")
        result = await manager.authorize(SimpleNamespace(name="write", is_read_only=False,
            check_permissions=lambda _: {"behavior": "ask"}), {})
        self.assertEqual(result.behavior, "deny")
        self.assertEqual(result.source, "session_rule")
        approve.assert_not_called()

    async def test_shared_policy_revokes_parent_and_child_remembered_grants(self):
        policy = ApprovalPolicy()
        approve = AsyncMock(return_value="allow")
        parent, child = self.manager(approve, policy), self.manager(approve, policy)
        for manager in (parent, child):
            manager.always_allow_tools.add("write")
        policy.set_mode("full")
        policy.set_mode("ask")
        for manager in (parent, child):
            await manager.authorize(SimpleNamespace(name="write", is_read_only=False), {})
            self.assertEqual(manager.always_allow_tools, set())
        self.assertEqual(approve.await_count, 2)
        restored = self.manager()
        restored.load_state(parent.export_state())
        self.assertEqual(restored.approval_policy.mode, "ask")

    async def test_full_access_keeps_plan_mode_limits_until_plan_exit(self):
        manager = self.manager(policy=ApprovalPolicy("full"), mode="plan")
        manager.always_allow_tools.add("file_write_tool")
        write = SimpleNamespace(name="file_write_tool", is_read_only=False)
        self.assertEqual((await manager.authorize(write, {"file_path": "/project/code.py"})).behavior, "deny")
        self.assertEqual((await manager.authorize(write, {"file_path": get_plan_file_path("fixture")})).behavior, "allow")
        self.assertEqual((await manager.authorize(SimpleNamespace(name="bash_tool", is_read_only=False), {"command": "echo test"})).behavior, "deny")

    async def test_invalid_mode_cannot_escalate_policy(self):
        policy = ApprovalPolicy()
        with self.assertRaises(ValueError):
            policy.set_mode("auto")
        self.assertEqual(policy.mode, "ask")
