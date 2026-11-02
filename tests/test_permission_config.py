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

    async def test_malformed_input_filters_never_fall_through_to_full_access(self):
        invalid = [{"case_sensitive": value, "contains": "PRIVATE"} for value in ("false", "true", 0, 1, None, [], {})]
        invalid += [{"path": value, "contains": "private"} for value in ([], {}, False, 1)]
        invalid += [{"key": [] , "contains": "private"}, {"source": "classifier_inpt", "contains": "private"}, {"contains": "private", "case_sensitve": False}, {"regex": 3}, {"glob": []}, {"contains": "private", "glob": "other"}, {}, {"equals": "private", "value": "other"}]
        invalid += [{operator: None} for operator in ("contains", "equals", "value")]
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "policy.json"
            for mode in ("ask", "full"):
                prompt = AsyncMock(return_value="allow")
                manager = PermissionManager(config_path=str(file), approval_policy=ApprovalPolicy(mode), prompt_handler=prompt)
                for matcher in invalid:
                    with self.subTest(mode=mode, matcher=matcher):
                        file.write_text(json.dumps({"alwaysDeny": [{"tools": ["fixture"], "input_matches": [matcher]}]}))
                        before = file.read_bytes()
                        decision = await manager.authorize(NS(name="fixture", is_read_only=False), {"path": "private"})
                        self.assertEqual(decision.behavior, "deny")
                        self.assertEqual(decision.source, "config_error")
                        self.assertEqual(file.read_bytes(), before)
                prompt.assert_not_awaited()
            for matcher in ({"contains": "PRIVATE", "case_sensitive": False}, {"regex": "PRIVATE", "case_sensitive": False}, {"glob": "*PRIVATE*", "case_sensitive": False}):
                file.write_text(json.dumps({"alwaysDeny": [{"input_matches": [{"path": "path", **matcher}]}]}))
                self.assertEqual((await manager.authorize(NS(name="fixture", is_read_only=False), {"path": "private"})).source, "config_rule")


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

    async def test_invalid_filter_cannot_write_a_file_and_valid_repair_still_denies(self):
        config = self.root / "permissions.json"
        self.engine.permission_manager.config_path = str(config)
        self.engine.permission_manager.approval_policy.set_mode("full")
        target = self.root / "owned-policy-probe.txt"
        def policy(value):
            config.write_text(json.dumps({"alwaysDeny": [{"tools": ["file_write_tool"], "input_matches": [{"path": "file_path", "contains": target.name.upper(), "case_sensitive": value}]}]}))
        policy("false")
        denied = await self.engine._execute_tool_wrapper({"id": "invalid-filter", "name": "file_write_tool", "input": {"file_path": str(target), "content": "must not write"}}, lambda _: None)
        self.assertTrue(denied["is_error"])
        self.assertIn("invalid_permission_config", denied["content"])
        self.assertFalse(target.exists())
        policy(False)
        denied = await self.engine._execute_tool_wrapper({"id": "repaired-filter", "name": "file_write_tool", "input": {"file_path": str(target), "content": "still denied"}}, lambda _: None)
        self.assertTrue(denied["is_error"])
        self.assertFalse(target.exists())
        config.write_text("{}")
        allowed = await self.engine._execute_tool_wrapper({"id": "explicitly-removed-filter", "name": "file_write_tool", "input": {"file_path": str(target), "content": "authorized"}}, lambda _: None)
        self.assertFalse(allowed["is_error"], allowed)
        self.assertEqual(target.read_text(), "authorized")
