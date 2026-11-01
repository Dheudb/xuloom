import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from xuloom.core.session_mutation import mutate
from xuloom.core.session_lock import SessionLock, SessionConflictError
from xuloom.core.storage import atomic_json


class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root / "workbench.json"
        self.env = {**os.environ, "XULOOM_DATA_DIR": str(self.root), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        environment = patch.dict(os.environ, self.env); environment.start(); self.addCleanup(environment.stop)

    def save(self, before, after):
        return mutate({"action": "workbench", "before": before, "after": after})["settings"]

    def test_stale_nested_and_directory_edits_merge_without_overwriting_other_window(self):
        before = {"projects": ["old"], "projectMeta": {"old": {"name": "keep", "pinned": False}}, "futureField": {"keep": True}}
        atomic_json(self.path, before)
        first = {**copy.deepcopy(before), "projects": ["first", "old"], "standaloneDirectories": ["chat-one"], "layout": {"sidebarWidth": 320}}
        second = {**copy.deepcopy(before), "projects": ["second", "old"], "standaloneDirectories": ["chat-two"], "layout": {"inspectorWidth": 550}}
        first["projectMeta"]["old"]["pinned"] = True
        second["projectMeta"]["old"]["name"] = "new name"
        self.save(before, first); result = self.save(before, second)
        self.assertEqual(set(result["projects"]), {"old", "first", "second"})
        self.assertEqual(set(result["standaloneDirectories"]), {"chat-one", "chat-two"})
        self.assertEqual(result["layout"], {"sidebarWidth": 320, "inspectorWidth": 550})
        self.assertEqual(result["projectMeta"]["old"], {"name": "new name", "pinned": True})
        self.assertEqual(result["futureField"], before["futureField"])

    def test_removing_project_is_not_undone_by_stale_unrelated_save_and_stale_edit_reports_conflict(self):
        before = {"projects": ["old", "keep"], "projectMeta": {"old": {"name": "old"}}}
        atomic_json(self.path, before)
        removed = {"projects": ["keep"], "projectMeta": {}}
        self.save(before, removed)
        result = self.save(before, {**before, "layout": {"sidebarWidth": 300}})
        self.assertEqual(result["projects"], ["keep"]); self.assertNotIn("old", result["projectMeta"])
        original = self.path.read_bytes()
        with self.assertRaises(SessionConflictError):
            self.save(before, {**before, "projectMeta": {"old": {"name": "stale rename"}}})
        self.assertEqual(self.path.read_bytes(), original)

    def test_remote_navigation_merges_windows_and_does_not_resurrect_deleted_reference(self):
        before = {"remoteChats": {"one": {"draft": "original", "pinned": False}}, "remoteProjects": {}}
        atomic_json(self.path, before)
        first = copy.deepcopy(before); first["remoteChats"]["two"] = {"draft": "another chat"}
        second = copy.deepcopy(before); second["remoteChats"]["one"]["draft"] = "unsent remote draft"
        self.save(before, first); result = self.save(before, second)
        self.assertEqual(set(result["remoteChats"]), {"one", "two"})
        self.assertEqual(result["remoteChats"]["one"]["draft"], "unsent remote draft")
        removed = copy.deepcopy(result); del removed["remoteChats"]["one"]
        self.save(result, removed)
        stale = copy.deepcopy(result); stale["remoteProjects"]["lab"] = {"collapsed": True}
        self.assertNotIn("one", self.save(result, stale)["remoteChats"])
        stale["remoteChats"]["one"]["draft"] = "stale overwrite"
        with self.assertRaises(SessionConflictError): self.save(result, stale)

    def test_workspace_selection_and_open_list_are_one_last_window_snapshot(self):
        before = {"activeConversation": None, "workspace": [], "layout": {"sidebarWidth": 280}}
        atomic_json(self.path, before)
        first = {**before, "activeConversation": {"project": "one", "resumeId": "one"}, "workspace": [{"project": "one", "resumeId": "one"}]}
        second = {**before, "activeConversation": {"project": "two", "resumeId": "two"}, "workspace": [{"project": "two", "resumeId": "two"}]}
        self.save(before, first); self.save(before, second)
        result = self.save(before, {**before, "layout": {"sidebarWidth": 330}})
        self.assertEqual(result["activeConversation"], second["activeConversation"])
        self.assertEqual(result["workspace"], second["workspace"])
        self.assertEqual(result["layout"]["sidebarWidth"], 330)

    def test_corrupt_file_and_failed_replace_preserve_original_settings(self):
        self.path.write_bytes(b"{broken")
        with self.assertRaises(ValueError): self.save({}, {"projects": ["new"]})
        self.assertEqual(self.path.read_bytes(), b"{broken")
        atomic_json(self.path, {"projects": ["old"]}); original = self.path.read_bytes()
        with patch("xuloom.desktop.settings.atomic_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): self.save({"projects": ["old"]}, {"projects": ["new", "old"]})
        self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_settings_upgrade_does_not_replace_another_instances_new_file(self):
        legacy = self.root / "legacy.json"; atomic_json(legacy, {"projects": ["legacy"]})
        result = mutate({"action": "workbench", "before": {}, "after": {}, "legacy": str(legacy)})
        self.assertEqual(result["settings"]["projects"], ["legacy"]); self.assertFalse(legacy.exists())
        atomic_json(legacy, {"projects": ["conflicting legacy"]})
        self.save({}, {"layout": {"sidebarWidth": 320}})
        result = mutate({"action": "workbench", "before": {}, "after": {}, "legacy": str(legacy)})
        self.assertEqual(result["settings"]["projects"], ["legacy"]); self.assertTrue(legacy.exists())

    def test_real_process_lock_denial_is_bounded_and_crash_releases_writer(self):
        request = self.root / "request.json"
        spec = {"before": {}, "after": {"projects": ["saved"]}}
        atomic_json(request, [spec])
        fixture = Path(__file__).with_name("fixtures") / "settings_writer.py"
        lock = SessionLock(self.root / "session-locks", "preferences_workbench"); lock.acquire()
        try:
            denied = subprocess.run([sys.executable, str(fixture), str(request), "write"], env=self.env, capture_output=True, timeout=15)
            self.assertNotEqual(denied.returncode, 0); self.assertFalse(self.path.exists())
        finally: lock.close()
        crashed = subprocess.run([sys.executable, str(fixture), str(request), "crash"], env=self.env, capture_output=True, timeout=15)
        self.assertEqual(crashed.returncode, 87, crashed.stderr)
        self.assertEqual(self.save({}, {"layout": {"sidebarWidth": 320}})["projects"], ["saved"])

    def test_real_concurrent_writers_preserve_all_stale_snapshot_project_additions(self):
        fixture = Path(__file__).with_name("fixtures") / "settings_writer.py"
        processes = []
        try:
            for label in ("one", "two"):
                request = self.root / (label + ".json")
                atomic_json(request, [{"before": {}, "after": {"projects": [label + str(index)], "standaloneDirectories": ["chat-" + label + str(index)]}} for index in range(12)])
                process = subprocess.Popen([sys.executable, str(fixture), str(request), "race"], env=self.env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                ready = queue.Queue(); threading.Thread(target=lambda stream=process.stdout, result=ready: result.put(stream.readline()), daemon=True).start()
                self.assertEqual(ready.get(timeout=10).strip(), "READY")
            for process in processes: process.stdin.write("go\n"); process.stdin.flush()
            for process in processes:
                out, err = process.communicate(timeout=20); self.assertEqual(process.returncode, 0, out + err)
            result = json.loads(self.path.read_bytes())
            self.assertEqual(set(result["projects"]), {label + str(index) for label in ("one", "two") for index in range(12)})
            self.assertEqual(len(result["standaloneDirectories"]), 24)
        finally:
            for process in processes:
                if process.poll() is None: process.kill(); process.wait(timeout=10)  # Only this test's own child.
                for stream in (process.stdin, process.stdout, process.stderr): stream.close()
