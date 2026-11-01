import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xuloom.core.session_lock import SessionConflictError
from xuloom.core.storage import atomic_json
from xuloom.desktop.bridge import DesktopBridge
from xuloom.desktop.settings import save_workbench
from xuloom.remote import navigation
from xuloom.remote.config import profile_key, validate_profile


class RemoteNavigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.data = self.root / "data"; self.path = self.data / "workbench.json"
        self.env = {**os.environ, "XULOOM_DATA_DIR": str(self.data), "XULOOM_CONFIG_DIR": str(self.root / "config"), "PYTHONIOENCODING": "utf-8"}
        env = patch.dict(os.environ, self.env); env.start(); self.addCleanup(env.stop)
        self.profile = validate_profile({"id": "lab", "name": "科学", "host": "offline-host", "cwd": "/work"})
        self.identity = "ssh:" + profile_key(self.profile) + ":saved"
        self.row = {"binding": self.profile, "sessionId": "saved", "project": "/work", "title": "Remote chat", "draft": "PRIVATE_DRAFT"}
        self.before = {"projects": ["local-project"], "remoteChats": {self.identity: self.row, "broken": {"SECRET": "PRIVATE_KEY"}},
                       "workspace": [{"project": "local-project", "resumeId": "local"}, {"remoteId": self.identity}], "activeConversation": {"remoteId": self.identity}}
        atomic_json(self.path, self.before)
        ssh = patch("xuloom.remote.ssh.SSHClient", side_effect=AssertionError("Navigation must never connect")); ssh.start(); self.addCleanup(ssh.stop)

    def test_inspection_redacts_raw_broken_rows_and_drafts_and_never_constructs_engine(self):
        self.before["remoteChats"][self.identity]["name"] = ["unknown future metadata"]
        atomic_json(self.path, self.before)
        bridge = DesktopBridge(lambda _: None, self.root)
        with patch.object(bridge, "ensure_engine", side_effect=AssertionError("No model")):
            result = asyncio.run(bridge.dispatch("remote.references", {}))["references"]
        self.assertEqual(len(result), 2); self.assertEqual(sum(item["valid"] for item in result), 1)
        self.assertEqual(next(item for item in result if item["valid"])["chatId"], self.identity)
        for private in ("PRIVATE_KEY", "PRIVATE_DRAFT", "binding", '"SECRET"'):
            self.assertNotIn(private, json.dumps(result))
        self.assertIsNone(bridge.engine)

    def test_forget_is_local_backs_up_original_and_only_clears_exact_selection(self):
        # These files model existing remote history/artifacts: no deletion primitive is used.
        artifact = self.root / "remote-history.json"; artifact.write_text("REMOTE_UNCHANGED")
        selected = next(row for row in navigation.references() if row["valid"])
        result = navigation.forget(selected["id"], selected["version"], confirm=True)
        saved = json.loads(self.path.read_bytes()); backup = json.loads(Path(result["backup"]).read_bytes())
        self.assertEqual(backup["key"], self.identity); self.assertEqual(backup["value"], self.row)
        self.assertEqual(saved["remoteChats"], {"broken": self.before["remoteChats"]["broken"]})
        self.assertEqual(saved["workspace"], [self.before["workspace"][0]]); self.assertIsNone(saved["activeConversation"])
        self.assertEqual(saved["projects"], self.before["projects"]); self.assertEqual(artifact.read_text(), "REMOTE_UNCHANGED")

    def test_confirmation_and_stale_ui_version_do_not_mutate_or_backup(self):
        selected = navigation.references()[0]; original = self.path.read_bytes()
        with self.assertRaises(ValueError): navigation.forget(selected["id"], selected["version"])
        self.assertEqual(original, self.path.read_bytes())
        newer = copy.deepcopy(self.before); newer["remoteChats"][self.identity]["draft"] = "NEW_UNSENT"
        atomic_json(self.path, newer)
        with self.assertRaises(SessionConflictError): navigation.forget(selected["id"], selected["version"], confirm=True)
        self.assertEqual(json.loads(self.path.read_bytes()), newer); self.assertFalse((self.data / "navigation-backups").exists())

    def test_concurrent_edit_after_inspection_is_rechecked_under_writer_lock(self):
        selected = next(row for row in navigation.references() if row["valid"])
        newer = copy.deepcopy(self.before); newer["remoteChats"][self.identity]["draft"] = "CONCURRENT_NEW_DRAFT"
        def race(spec):
            atomic_json(self.path, newer)
            return save_workbench(spec)
        with patch.object(navigation, "save_workbench", side_effect=race):
            with self.assertRaises(SessionConflictError): navigation.forget(selected["id"], selected["version"], confirm=True)
        self.assertEqual(json.loads(self.path.read_bytes()), newer)

    def test_backup_failure_and_publish_failure_preserve_original_navigation(self):
        selected = navigation.references()[0]; original = self.path.read_bytes()
        with patch("xuloom.desktop.settings.atomic_text", side_effect=PermissionError("backup denied")):
            with self.assertRaises(PermissionError): navigation.forget(selected["id"], selected["version"], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)
        with patch("xuloom.desktop.settings.atomic_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): navigation.forget(selected["id"], selected["version"], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)
        backups = list((self.data / "navigation-backups").glob("*.json")); self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_bytes())["value"], self.row)

    def test_invalid_containers_are_recoverable_without_losing_local_selection(self):
        for value in (None, False, [], "broken"):
            original = {"remoteChats": value, "activeConversation": {"project": "local", "resumeId": "keep"}, "workspace": [{"project": "local"}, {"remoteId": self.identity}]}
            atomic_json(self.path, original); selected = navigation.references()[0]
            self.assertFalse(selected["valid"]); self.assertEqual(selected["kind"], "container")
            navigation.forget(selected["id"], selected["version"], confirm=True)
            saved = json.loads(self.path.read_bytes()); self.assertEqual(saved["remoteChats"], {})
            self.assertEqual(saved["activeConversation"], original["activeConversation"]); self.assertEqual(saved["workspace"], [{"project": "local"}])

    def test_invalid_display_group_recovery_does_not_remove_chats(self):
        original = {**self.before, "remoteProjects": {"ssh:" + profile_key(self.profile): {"archived": "bad", "name": ["bad"]}}}
        atomic_json(self.path, original); selected = next(row for row in navigation.references() if row["field"] == "remoteProjects")
        self.assertFalse(selected["valid"])
        navigation.forget(selected["id"], selected["version"], confirm=True)
        saved = json.loads(self.path.read_bytes()); self.assertEqual(saved["remoteChats"], original["remoteChats"])
        self.assertEqual(saved["activeConversation"], original["activeConversation"]); self.assertEqual(saved["remoteProjects"], {})

    def test_malformed_rendering_fields_are_not_valid_rows(self):
        for field, value in (("title", []), ("created", True), ("updated", {}), ("permissionMode", []), ("draftAttachments", ["local-file"])):
            row = {**self.row, field: value}; atomic_json(self.path, {"remoteChats": {self.identity: row}})
            self.assertFalse(navigation.references()[0]["valid"], field)

    def test_real_cli_works_with_no_ssh_profiles_and_requires_confirmation(self):
        def command(*args):
            result = subprocess.run([sys.executable, "-m", "xuloom", "remote", *args, "--json"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", timeout=30)
            return result, json.loads(result.stdout.strip().splitlines()[-1])
        process, receipt = command("references"); self.assertEqual(process.returncode, 0, process.stderr)
        selected = next(row for row in receipt["result"]["references"] if not row["valid"])
        original = self.path.read_bytes()
        process, _ = command("forget", selected["id"], "--version", selected["version"])
        self.assertNotEqual(process.returncode, 0); self.assertEqual(self.path.read_bytes(), original)
        process, receipt = command("forget", selected["id"], "--version", selected["version"], "--confirm")
        self.assertEqual(process.returncode, 0, process.stderr); self.assertTrue(receipt["result"]["removed"])
        self.assertNotIn("broken", json.loads(self.path.read_bytes())["remoteChats"])

    def test_stale_generic_delete_cannot_discard_newer_remote_draft(self):
        after = copy.deepcopy(self.before); del after["remoteChats"][self.identity]
        newer = copy.deepcopy(self.before); newer["remoteChats"][self.identity]["draft"] = "NEW_DRAFT"
        atomic_json(self.path, newer)
        with self.assertRaises(SessionConflictError): save_workbench({"before": self.before, "after": after})
        self.assertEqual(json.loads(self.path.read_bytes()), newer)

    def test_damaged_unicode_can_be_inspected_and_backed_up_without_poisoning_recovery(self):
        # JSON can carry isolated UTF-16 surrogates, which cannot be emitted as UTF-8.
        before = {"remoteChats": {"broken\ud800": {"draft": "bad\udfff"}, self.identity: {**self.row, "title": "bad\ud800"}}}
        self.path.write_text(json.dumps(before, ensure_ascii=True), encoding="utf-8")
        rows = navigation.references(); self.assertEqual(len(rows), 2); self.assertFalse(any(row["valid"] for row in rows))
        json.dumps(rows, ensure_ascii=False).encode("utf-8")
        for row in rows:
            result = navigation.forget(row["id"], row["version"], confirm=True)
            backup = json.loads(Path(result["backup"]).read_bytes()); self.assertEqual(backup["value"], before["remoteChats"][backup["key"]])
        self.assertEqual(json.loads(self.path.read_bytes()), {"remoteChats": {}})
