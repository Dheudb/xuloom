import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from xuloom.security.permissions import PermissionManager, ApprovalPolicy
import tests.test_tool_recovery as recovery


class PermissionConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_configuration_never_grants_access_and_recovers_without_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "permissions.json"
            for mode in ("full", "ask"):
                prompt = AsyncMock(return_value="allow")
                manager = PermissionManager(config_path=str(file), approval_policy=ApprovalPolicy(mode), prompt_handler=prompt)
                manager.always_allow_tools.add("fixture")
                for text in ('{bad-private-value', '[]', 'null', '{"alwaysDeny": ["fixture"], "alwaysDeny": []}',
                    '{"alwaysDeny": "fixture"}', '{"rules": [false]}', '{"rules": [{"behavior": "dney"}]}',
                    '{"alwaysDeny": [{"tools": "fixture"}]}', '{"alwaysDeny": [{"input_matches": [{"regex": "["}]}]}'):
                    file.write_text(text)
                    for read_only in (True, False):
                        decision = await manager.authorize(NS(name="fixture", is_read_only=read_only), {})
                        self.assertEqual(decision.behavior, "deny")
                        self.assertEqual(decision.source, "config_error")
                        self.assertNotIn("private-value", decision.reason)
                self.assertEqual(manager.consecutive_denials.get("fixture", 0), 0)
                prompt.assert_not_awaited()
                file.write_text("{}")
                self.assertEqual((await manager.authorize(NS(name="fixture", is_read_only=True), {})).behavior, "allow")

    async def test_unreadable_existing_policy_is_not_treated_as_missing(self):
        manager = PermissionManager(config_path="fixture-policy.json", approval_policy=ApprovalPolicy("full"))
        with patch("builtins.open", side_effect=PermissionError("private raw diagnostic")):
            result = await manager.authorize(NS(name="fixture", is_read_only=True), {})
        self.assertEqual(result.source, "config_error")
        self.assertNotIn("private raw diagnostic", result.reason)

    async def test_missing_policy_retains_default_behavior_and_explicit_deny_still_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "policy.json"
            manager = PermissionManager(config_path=str(file), approval_policy=ApprovalPolicy("full"))
            tool = NS(name="fixture", is_read_only=True)
            self.assertEqual((await manager.authorize(tool, {})).behavior, "allow")
            file.write_text(json.dumps({"alwaysDeny": ["fixture"]}))
            self.assertEqual((await manager.authorize(tool, {})).behavior, "deny")

    async def test_aliases_use_one_policy_snapshot_per_authorization(self):
        manager = PermissionManager(config_path="missing-policy.json", approval_policy=ApprovalPolicy("full"))
        tool = NS(name="fixture", is_read_only=True, permission_aliases={"alias": {}})
        with patch.object(manager, "_load_config", side_effect=[{"alwaysDeny": ["alias"]}, {}]) as load:
            decision = await manager.authorize(tool, {})
        self.assertEqual(decision.behavior, "deny"); load.assert_called_once()


class PermissionConfigExecutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    async def test_corrupt_policy_prevents_real_file_write_and_repair_allows_it(self):
        config = self.root / "permissions.json"
        config.write_text("{private-invalid-policy")
        self.engine.permission_manager.config_path = str(config)
        self.engine.permission_manager.approval_policy.set_mode("full")
        target = self.root / "authorized-after-repair.txt"
        for index in range(4):
            receipt = await self.engine._execute_tool_wrapper({"id": "denied-" + str(index), "name": "file_write_tool",
                "input": {"file_path": str(target), "content": "saved"}}, lambda _: None)
            self.assertTrue(receipt["is_error"])
            self.assertIn("invalid_permission_config", receipt["content"])
            self.assertNotIn("private-invalid-policy", receipt["content"])
            self.assertFalse(target.exists())
        config.write_text("{}")
        receipt = await self.engine._execute_tool_wrapper({"id": "after-repair", "name": "file_write_tool",
            "input": {"file_path": str(target), "content": "saved"}}, lambda _: None)
        self.assertFalse(receipt["is_error"], receipt)
        self.assertEqual(target.read_text(), "saved")
