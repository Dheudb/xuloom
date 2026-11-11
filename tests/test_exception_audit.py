"""Regression tests for broad exception handlers that used to fail open or lose data."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.core.hooks import HookManager
from xuloom.features.content_replacement import ContentReplacementManager
from xuloom.security.permissions import PermissionManager, PermissionRequest


class HookConfigurationFailuresTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hooks.json"
        self.manager = HookManager(str(self.path), cwd=self.temp.name)
        self.payload = {"tool_name": "bash_tool", "tool_use_id": "t1", "tool_input": {"command": "rm -rf build"}}

    def tearDown(self):
        self.temp.cleanup()

    def test_unreadable_configuration_rejects_tool_calls_instead_of_dropping_rules(self):
        self.path.write_text('{"BeforeToolUse": [{"tools": ["bash_tool"], "behavior": "reject"', encoding="utf-8")
        with self.assertLogs("xuloom.core.hooks", "WARNING"):
            decision = self.manager.evaluate_before_tool_hooks(self.payload)
        self.assertEqual(decision.behavior, "reject")
        self.assertIn("could not be read", decision.reason)
        self.assertTrue(self.manager.has_configured("BeforeToolUse"))

    def test_failing_rule_rejects_the_call(self):
        self.path.write_text(json.dumps({"BeforeToolUse": [{"tools": ["bash_tool"], "behavior": "allow",
                                                            "updated_input": {"command": "x"}}]}), encoding="utf-8")
        with patch.object(HookManager, "_render_structured_value", side_effect=RuntimeError("bad template")), \
                self.assertLogs("xuloom.core.hooks", "WARNING"):
            decision = self.manager.evaluate_before_tool_hooks(self.payload)
        self.assertEqual(decision.behavior, "reject")
        self.assertIn("hook #1 failed", decision.reason)

    def test_valid_and_missing_configuration_are_unchanged(self):
        self.assertEqual(self.manager.evaluate_before_tool_hooks(self.payload).behavior, "allow")
        self.path.write_text(json.dumps({"BeforeToolUse": [{"tools": ["other_tool"], "behavior": "reject"}]}), encoding="utf-8")
        self.assertEqual(self.manager.evaluate_before_tool_hooks(self.payload).behavior, "allow")

    def test_other_events_report_unreadable_configuration(self):
        self.path.write_text("{", encoding="utf-8")
        with self.assertLogs("xuloom.core.hooks", "WARNING") as logs:
            self.assertEqual(self.manager.execute("AfterToolUse", self.payload), [])
            self.manager.execute("AfterToolUse", self.payload)
        self.assertEqual(len(logs.output), 1)  # reported once, not on every call


class ClassifierFailureTests(unittest.TestCase):
    def test_classifier_error_requires_approval(self):
        class Broken:
            def classify(self, request):
                raise RuntimeError("model unavailable")
        manager = PermissionManager(classifier_manager=Broken(), config_path=os.devnull)
        request = PermissionRequest("bash_tool", "run", "high", {}, "default", False, "", {})
        with self.assertLogs("xuloom.permissions", "WARNING"):
            decision = manager._run_auto_classifier(request)
        self.assertEqual(decision.behavior, "ask")
        self.assertEqual(decision.source, "classifier_error")


class SpillFailureTests(unittest.TestCase):
    def test_tool_result_stays_inline_when_it_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            manager = ContentReplacementManager(base_dir=str(blocker), session_id="s")
            original = "line\n" * 2000
            messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": original}]}]
            with self.assertLogs("xuloom.context", "WARNING"):
                replaced = manager.enforce_budget(messages, budget=1000)
        self.assertEqual(replaced, 0)
        self.assertEqual(messages[0]["content"][0]["content"], original)

    def test_successful_spill_still_replaces_with_a_real_file(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = ContentReplacementManager(base_dir=folder, session_id="s")
            original = "line\n" * 2000
            messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": original}]}]
            self.assertEqual(manager.enforce_budget(messages, budget=1000), 1)
            text = messages[0]["content"][0]["content"]
            path = text.split("Full output saved to: ", 1)[1].split("\n", 1)[0]
            self.assertEqual(Path(path).read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
