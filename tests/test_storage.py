import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xuloom.core.session import SessionManager
from xuloom.core.storage import session_directory, history_root, atomic_text


class StorageTests(unittest.TestCase):
    def test_deep_central_data_directory_preserves_sessions_memory_and_writer_lock(self):
        import shutil
        from xuloom.core.storage import IOPath
        from xuloom.core.session_lock import SessionBusyError
        from xuloom.memory.session_memory import SessionMemoryManager
        with tempfile.TemporaryDirectory(prefix='xuloom-deep-central-') as temp:
            root = Path(temp); workspace = root / 'workspace'; workspace.mkdir()
            manager = None
            try:
                with patch.dict(os.environ, {'XULOOM_DATA_DIR': str(root / ('a' * 100) / ('b' * 100)), 'HOME': str(root), 'USERPROFILE': str(root)}):
                    manager = SessionManager(cwd=workspace)
                    self.assertGreater(len(manager.session_dir), 260)
                    self.assertTrue(manager.save_session_state('owned-session', [{'role': 'user', 'content': 'full history'}]), manager.last_save_error)
                    self.assertTrue(manager.save_subagent_transcript('owned-child', {'session_id': 'owned-session', 'messages': ['full result']}), manager.last_save_error)
                    self.assertEqual(manager.load_subagent_transcript('owned-child')['messages'], ['full result'])
                    self.assertEqual(manager.list_session_ids(), ['owned-session'])
                    self.assertIn('owned-session', manager.get_recent_sessions())
                    self.assertEqual(os.fspath(session_directory(workspace)), str(session_directory(workspace)))
                    from xuloom.features.session_summary import read_summary
                    self.assertEqual(read_summary(workspace, 'owned-session')['sessionId'], 'owned-session')
                    second = SessionManager(cwd=workspace)
                    self.assertEqual(second.load_session('owned-session')[0]['content'], 'full history')
                    with self.assertRaises(SessionBusyError): second.acquire_writer('owned-session')
                    manager.release_writer(); second.acquire_writer('owned-session'); second.release_writer()
                    memory = SessionMemoryManager('owned-session')
                    memory._write_notes('Preserve the user goal and evidence.')
                    self.assertEqual(memory.get_memory_content(), 'Preserve the user goal and evidence.')
                    self.assertTrue(manager.delete_session('owned-session'))
                    self.assertFalse(IOPath(manager.get_subagent_transcript_path('owned-child')).exists())
            finally:
                if manager: manager.release_writer()
                self.assertEqual(root.resolve().parent, Path(tempfile.gettempdir()).resolve())
                shutil.rmtree(IOPath(root))

    def test_long_history_paths_are_read_copied_and_deleted_without_hidden_files(self):
        from xuloom.core.storage import IOPath, atomic_copy, atomic_text, file_hash
        from xuloom.core import session_mutation as actor
        import shutil
        with tempfile.TemporaryDirectory(prefix="xuloom-long-path-") as temp, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(temp) / "data")}):
            root = Path(temp); workspace = root / "workspace"; workspace.mkdir()
            manager = SessionManager(cwd=workspace)
            try:
                manager.save_session_state("long-history", [{"role": "user", "content": "owned"}])
                folder = history_root(workspace) / "tool-results/long-history" / ("a" * 120) / ("b" * 120)
                source, target = IOPath(folder / "source.txt"), IOPath(folder / "copy.txt")
                atomic_text(source, "complete evidence"); atomic_copy(target, source, file_hash(source))
                self.assertEqual(target.read_text(encoding="utf8"), "complete evidence")
                self.assertNotIn("\\\\?\\", str(target))
                self.assertEqual({p.name for p in actor._files(folder.parent)}, {"source.txt", "copy.txt"})
                manager.delete_session("long-history")
                self.assertFalse(IOPath(folder.parent).exists())
            finally:
                manager.release_writer()
                # Only this explicit temporary root; fail loudly if cleanup was incomplete.
                assert root.parent.resolve() == Path(tempfile.gettempdir()).resolve() and root.name.startswith("xuloom-long-path-")
                shutil.rmtree(IOPath(root))

    def test_changed_streamed_copy_never_publishes_unverified_bytes(self):
        from xuloom.core.storage import atomic_copy, file_hash
        from xuloom.core.session_lock import SessionConflictError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; target = root / "target"
            source.write_bytes(b"original"); expected = file_hash(source)
            source.write_bytes(b"changed"); target.write_bytes(b"previous")
            with self.assertRaises(SessionConflictError): atomic_copy(target, source, expected)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertEqual({item.name for item in root.iterdir()}, {"source", "target"})

    def test_migration_preflights_existing_writer_and_preserves_legacy_until_release(self):
        from xuloom.core.session_lock import SessionBusyError
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(temp) / "data")}):
            root = Path(temp) / "workspace"; root.mkdir()
            manager = SessionManager(cwd=root)
            try:
                manager.save_session_state("shared", [{"role": "user", "content": "central"}])
                legacy = root / ".xuloom/tool-results/shared/result.txt"
                legacy.parent.mkdir(parents=True); legacy.write_text("legacy output", encoding="utf-8")
                with self.assertRaises(SessionBusyError): SessionManager(cwd=root)
                self.assertTrue(legacy.exists())
                self.assertFalse((history_root(root) / "tool-results/shared/result.txt").exists())
                manager.release_writer()
                SessionManager(cwd=root)
                self.assertFalse(legacy.exists())
                self.assertEqual((history_root(root) / "tool-results/shared/result.txt").read_text(), "legacy output")
            finally:
                manager.release_writer()

    def test_migration_journal_cannot_authorize_removing_a_source_with_missing_destination(self):
        from xuloom.core.storage import atomic_json
        import hashlib
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(temp) / "data")}):
            root = Path(temp) / "workspace"; root.mkdir()
            legacy = root / ".xuloom/tool-results/shared/result.txt"
            legacy.parent.mkdir(parents=True); legacy.write_bytes(b"full output")
            atomic_json(history_root(root) / "migration.json", {str(legacy.resolve()): hashlib.sha256(b"full output").hexdigest()})
            SessionManager(cwd=root)
            self.assertEqual((history_root(root) / "tool-results/shared/result.txt").read_bytes(), b"full output")
            self.assertFalse(legacy.exists())

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation retry")
    def test_transient_windows_replace_lock_is_retried_without_losing_content(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "session.json"
            target.write_text("old", encoding="utf-8")
            real_replace = os.replace
            calls = []
            def replace(source, destination):
                calls.append(source)
                if len(calls) <= 2:
                    error = PermissionError("temporarily held by reader")
                    error.winerror = 32
                    raise error
                real_replace(source, destination)
            with patch("xuloom.core.storage.os.replace", side_effect=replace), patch("xuloom.core.storage.time.sleep"):
                atomic_text(target, "new\nUnicode α")
            self.assertEqual(target.read_bytes(), "new\nUnicode α".encode())
            self.assertEqual(len(calls), 3)
            self.assertEqual(list(Path(temp).iterdir()), [target])

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation retry")
    def test_permanent_replace_failure_is_bounded_and_keeps_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "session.json"
            target.write_text("old", encoding="utf-8")
            error = PermissionError("permanent denial")
            error.winerror = 5
            with patch("xuloom.core.storage.os.replace", side_effect=error) as replace, patch("xuloom.core.storage.time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_text(target, "new")
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(list(Path(temp).iterdir()), [target])

    def test_cli_migration_preserves_metadata_and_artifact_references(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(temp) / "data")}):
            root = Path(temp) / "workspace"; root.mkdir()
            old = root / ".xuloom"; (old / "sessions/subagents").mkdir(parents=True)
            proof = old / "tool-results/session/result.txt"; proof.parent.mkdir(parents=True); proof.write_text("large result", encoding="utf-8")
            (old / "sessions/session.json").write_text(json.dumps({"messages": [{"role": "user", "content": str(proof)}], "metadata": {"turn_traces": [{"id": "turn"}]}}), encoding="utf-8")
            (old / "sessions/subagents/child.json").write_text('{"session_id":"session"}', encoding="utf-8")
            (old / "session-index.json").write_text('{"version":1,"sessions":{"session":{"draft":"draft","archived":true}}}', encoding="utf-8")
            (old / "capabilities.json").write_text("{}", encoding="utf-8")
            manager = SessionManager(cwd=root); stored = manager.load_session_state("session")
            self.assertEqual(stored["metadata"]["turn_traces"], [{"id": "turn"}])
            self.assertIn(str(history_root(root) / "tool-results/session/result.txt"), stored["messages"][0]["content"])
            self.assertEqual(json.loads((history_root(root) / "index.json").read_text())["sessions"]["session"]["draft"], "draft")
            self.assertTrue((Path(manager.session_dir) / "subagents/child.json").exists())
            self.assertFalse((old / "sessions").exists()); self.assertTrue((old / "capabilities.json").exists())
            self.assertEqual(str(session_directory(root)), manager.session_dir)
            self.assertTrue(manager.save_session_state("session", [{"role": "assistant", "content": "continued"}]))
            self.assertFalse((old / "sessions").exists())
            manager.release_writer()

    def test_conflicting_history_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(temp) / "data")}):
            root = Path(temp) / "workspace"; root.mkdir(); manager = SessionManager(cwd=root)
            manager.save_session_state("same", [{"role": "user", "content": "central"}])
            legacy = root / ".xuloom/sessions/same.json"; legacy.parent.mkdir(parents=True)
            legacy.write_text('{"messages":[{"role":"user","content":"legacy"}]}', encoding="utf-8")
            manager.release_writer()
            with self.assertRaisesRegex(ValueError, "冲突"): SessionManager(cwd=root)
            self.assertTrue(legacy.exists()); self.assertEqual(manager.load_session_state("same")["messages"][0]["content"], "central")
            manager.release_writer()
