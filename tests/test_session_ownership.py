import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionBusyError, SessionConflictError, SessionLock
from xuloom.desktop.bridge import DesktopBridge


class SessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {**os.environ, "XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"), "OPENAI_API_KEY": "fixture", "XULOOM_MODEL_PROVIDER": "openai"}
        env = patch.dict(os.environ, self.environment)
        env.start()
        self.addCleanup(env.stop)
        self.managers = []
        self.addCleanup(lambda: [manager.release_writer() for manager in self.managers])

    def manager(self):
        manager = SessionManager(cwd=self.root)
        self.managers.append(manager)
        return manager

    def holder(self):
        script = Path(__file__).with_name("fixtures") / "session_writer.py"
        env = {**self.environment, "PYTHONPATH": os.environ.get("XULOOM_TEST_SITE", str(Path(__file__).resolve().parents[1]))}
        process = subprocess.Popen([sys.executable, str(script), str(self.root), "hold", "shared"], env=env,
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.communicate("release\n", timeout=30)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.addCleanup(cleanup)
        self.assertEqual(process.stdout.readline().strip(), "OWNED")
        return process, script, env

    def test_other_process_can_read_but_cannot_request_model_or_execute_tool(self):
        process, script, env = self.holder()
        reader = self.manager()
        self.assertTrue(reader.load_session_state("shared")["_valid"])
        path = Path(reader.session_dir) / "shared.json"
        before = path.read_bytes()
        from xuloom.core.storage import artifact_directory
        spill = artifact_directory(self.root) / "shared" / "new-unregistered-result.txt"
        spill.parent.mkdir(parents=True)
        spill.write_text("Another writer's new result", encoding="utf-8")
        probe = subprocess.run([sys.executable, str(script), str(self.root), "probe", "shared"], env=env,
            cwd=self.root, capture_output=True, text=True, timeout=45)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        result = json.loads(probe.stdout.strip().splitlines()[-1])
        self.assertEqual(result, {"reason": "session_busy", "model_calls": 0, "tool_error": True, "agent_error": "session_busy", "effect_exists": False})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(spill.read_text(encoding="utf-8"), "Another writer's new result", "Read-only recovery must not clean another writer's output")
        with self.assertRaises(SessionBusyError):
            reader.delete_session("shared")

    def test_os_releases_lease_after_actual_process_crash(self):
        process, _, _ = self.holder()
        contender = self.manager()
        with self.assertRaises(SessionBusyError):
            contender.acquire_writer("shared")
        process.communicate("crash\n", timeout=20)
        self.assertEqual(process.returncode, 87)
        self.assertTrue(contender.load_session_state("shared")["_valid"])
        self.assertTrue(contender.save_session_state("shared", [{"role": "user", "content": "recovered"}]), contender.last_save_error)

    def test_stale_snapshot_cannot_overwrite_a_newer_checkpoint_after_release(self):
        first, stale = self.manager(), self.manager()
        self.assertTrue(first.save_session_state("shared", [{"role": "user", "content": "old"}]))
        stale.load_session_state("shared")
        self.assertFalse(stale.save_session_state("shared", []))
        self.assertIn("another writer", stale.last_save_error)
        self.assertTrue(first.save_session_state("shared", [{"role": "user", "content": "latest"}]))
        first.release_writer()
        self.assertFalse(stale.save_session_state("shared", []))
        self.assertIn("changed since", stale.last_save_error)
        self.assertFalse(stale.owns_writer("shared"), "A rejected stale save must release its newly acquired lease")
        self.assertEqual(stale.load_session_state("shared")["messages"][0]["content"], "latest")
        self.assertTrue(stale.save_session_state("shared", [{"role": "user", "content": "continued"}]))
        raw = json.loads((Path(stale.session_dir) / "shared.json").read_text())
        self.assertEqual(raw["revision"], 3)

    def test_history_listing_does_not_mask_a_stale_execution_snapshot(self):
        first, reader = self.manager(), self.manager()
        first.save_session_state("shared", [{"role": "user", "content": "old"}])
        reader.load_session_state("shared")
        first.save_session_state("shared", [{"role": "user", "content": "latest"}])
        first.release_writer()
        self.assertIn("shared", reader.get_recent_sessions())
        self.assertFalse(reader.save_session_state("shared", []))
        self.assertIn("changed since", reader.last_save_error)

    def test_batch_delete_preflights_all_writers_and_preserves_every_record_when_busy(self):
        process, _, _ = self.holder()
        writer, deleter = self.manager(), self.manager()
        writer.save_session_state("available", [])
        writer.release_writer()
        with self.assertRaises(SessionBusyError):
            deleter.clear_all_sessions()
        self.assertEqual(set(deleter.list_session_ids()), {"available", "shared"})
        self.assertFalse(deleter.owns_writer("available"))
        process.communicate("release\n", timeout=20)
        self.assertEqual(deleter.clear_all_sessions(), 2)
        self.assertEqual(deleter.list_session_ids(), [])
        self.assertFalse(deleter.owns_writer("shared"))

    def test_desktop_storage_actor_and_engine_writer_use_the_same_os_lock(self):
        async def verify():
            bridge = DesktopBridge(lambda _: None, cwd=self.root)
            contender = self.manager()
            self.assertTrue(contender.save_session_state("shared", []))
            spec = {"action": "delete", "sessions": [{"project": str(self.root), "session_id": "shared"}]}
            with self.assertRaises(SessionBusyError):
                await bridge.dispatch("storage.mutate", spec)
            checkpoint = Path(contender.session_dir) / "shared.json"
            self.assertTrue(checkpoint.exists())
            contender.release_writer()
            self.assertTrue((await bridge.dispatch("storage.mutate", spec))["completed"])
            self.assertFalse(checkpoint.exists())
            await bridge.close()
        asyncio.run(verify())

    def test_desktop_rejects_corrupt_resume_before_initialization_and_closes_readonly_engine(self):
        async def verify():
            manager = self.manager()
            self.assertTrue(manager.save_session_state("corrupt", []))
            manager.release_writer()
            checkpoint = Path(manager.session_dir) / "corrupt.json"
            raw = json.loads(checkpoint.read_bytes()); raw["revision"] = None
            checkpoint.write_text(json.dumps(raw), encoding="utf-8")
            before = checkpoint.read_bytes()
            bridge = DesktopBridge(lambda _: None, cwd=self.root)
            bridge.resume_id = "corrupt"
            try:
                with patch.object(bridge, "_create_engine", wraps=bridge._create_engine) as create:
                    with self.assertRaises(SessionConflictError): await bridge.ensure_engine(write=True)
                    create.assert_not_called()
                engine = bridge._create_engine()
                with patch.object(bridge, "_create_engine", return_value=engine), \
                        patch.object(engine, "aclose", wraps=engine.aclose) as close, \
                        patch.object(bridge, "sync_model", new_callable=AsyncMock) as sync, \
                        patch.object(bridge, "_connect_mcp", new_callable=AsyncMock) as connect:
                    with self.assertRaises(SessionConflictError): await bridge.ensure_engine()
                    close.assert_awaited_once()
                    sync.assert_not_awaited()
                    connect.assert_not_awaited()
                self.assertIsNone(bridge.engine)
                self.assertIsNone(bridge.protocol_task)
                self.assertEqual(checkpoint.read_bytes(), before)
            finally:
                await bridge.close()
        asyncio.run(verify())

    def test_desktop_failed_model_configuration_releases_new_engine_writer(self):
        async def verify():
            bridge = DesktopBridge(lambda _: None, cwd=self.root)
            engine = bridge._create_engine()
            try:
                with patch.object(bridge, "_create_engine", return_value=engine), \
                        patch.object(engine, "aclose", wraps=engine.aclose) as close, \
                        patch.object(bridge, "sync_model", new_callable=AsyncMock, side_effect=ValueError("bad configuration")):
                    with self.assertRaisesRegex(ValueError, "bad configuration"):
                        await bridge.ensure_engine(write=True)
                    close.assert_awaited_once()
                self.assertIsNone(bridge.engine)
                self.assertFalse(engine.session_manager.owns_writer(engine.session_id))
                self.manager().acquire_writer(engine.session_id)
            finally:
                await bridge.close()
        asyncio.run(verify())
