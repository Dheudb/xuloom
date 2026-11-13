"""Small harness contracts learned from the public source comparisons."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import sys
import hashlib
import unittest
from unittest.mock import patch

import httpx
from xuloom.agents.agent_tool import AgentTool
from xuloom.context.file_state_cache import FileStateCache
from xuloom.context.tool_results import serialize_tool_result
from xuloom.features.incremental_write_queue import IncrementalWriteQueue
from xuloom.tools.file_write_tool import FileWriteTool
from xuloom.tools.file_edit_tool import FileEditTool
from xuloom.tools.notebook_edit_tool import NotebookEditTool
from xuloom.tools.repl_tool import ReplTool
from xuloom.tools.web_tool import WebFetchTool
from xuloom.utils.command_sessions import CommandSessions


class ParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_atomic_replace_preserves_original_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"; path.write_text("original")
            tool = FileWriteTool(); tool.context = {"read_file_state": {str(path): True}}
            with patch("xuloom.core.storage.os.replace", side_effect=OSError("injected publication failure")):
                result = await tool.execute(file_path=str(path), content="replacement")
            self.assertTrue(result["is_error"])
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(list(Path(directory).iterdir()), [path])
            result = await tool.execute(file_path=str(path), content="invalid surrogate \ud800")
            self.assertTrue(result["is_error"])
            self.assertEqual(path.read_text(), "original")

    async def test_stale_write_and_edit_refuse_to_overwrite_external_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            cache = FileStateCache()
            for tool in [FileWriteTool(), FileEditTool()]:
                path.write_text("observed old token")
                cache.record_read(str(path), kind="text")
                path.write_text("external old token")
                tool.context = {"file_state_cache": cache}
                inputs = {"content": "new"} if tool.name == "file_write_tool" else {"old_string": "old", "new_string": "new"}
                result = await tool.execute(file_path=str(path), **inputs)
                self.assertTrue(result["is_error"])
                self.assertIn("changed since", result["content"])
                self.assertEqual(path.read_text(), "external old token")

    async def test_new_file_is_rechecked_after_waiting_in_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            queue = IncrementalWriteQueue(); _, lock = queue._lock_for(str(path))
            await lock.acquire()
            tool = FileWriteTool(); tool.context = {"incremental_write_queue": queue}
            task = asyncio.create_task(tool.execute(file_path=str(path), content="overwrite"))
            try:
                await asyncio.sleep(.01); path.write_text("created by another operation")
            finally: lock.release()
            result = await task
            self.assertTrue(result["is_error"])
            self.assertEqual(path.read_text(), "created by another operation")

    async def test_notebook_read_modify_write_is_inside_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.ipynb"
            path.write_text(json.dumps({"cells": [{"id": "a", "cell_type": "markdown", "source": "a"},
                {"id": "b", "cell_type": "markdown", "source": "b"}]}))
            queue = IncrementalWriteQueue(); _, lock = queue._lock_for(str(path)); await lock.acquire()
            tool = NotebookEditTool(); tool.context = {"read_file_state": {str(path): True}, "incremental_write_queue": queue}
            first = asyncio.create_task(tool.execute(notebook_path=str(path), cell_id="a", new_source="A"))
            second = asyncio.create_task(tool.execute(notebook_path=str(path), cell_id="b", new_source="B"))
            await asyncio.sleep(.01); lock.release()
            await asyncio.gather(first, second)
            self.assertEqual([c["source"] for c in json.loads(path.read_text())["cells"]], ["A", "B"])

    def test_parent_shares_mutation_queue_but_not_worker_observations(self):
        from types import SimpleNamespace
        parent_cache, child_cache, queue = FileStateCache(), FileStateCache(), IncrementalWriteQueue()
        tool = AgentTool(); tool.context = {"file_state_cache": parent_cache, "incremental_write_queue": queue}
        child = SimpleNamespace(file_state_cache=child_cache, tool_context={"file_state_cache": child_cache})
        tool._share_runtime_managers(child)
        self.assertIs(child.incremental_write_queue, queue)
        self.assertIs(child.tool_context["file_state_cache"], child_cache)

    async def test_repl_cancellation_stops_owned_code(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = ReplTool(); tool.context = {"session_cwd": directory}
            # Test a pending side effect, independently of machine load.
            code = "import time; from pathlib import Path; Path('ready').write_text('ready'); exec(\"while not Path('release').exists(): time.sleep(.02)\"); Path('late').write_text('bad')"
            work = asyncio.create_task(tool.execute(language="python", code=code))
            async def ready():
                while not (Path(directory) / "ready").exists(): await asyncio.sleep(.02)
            try:
                await asyncio.wait_for(ready(), 20)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError): await work
            finally:
                (Path(directory) / "release").write_text("release")
                if not work.done():
                    work.cancel(); await asyncio.gather(work, return_exceptions=True)
            await asyncio.sleep(1)
            self.assertFalse((Path(directory) / "late").exists())

    async def fetch(self, response):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
        with patch("xuloom.core.network.model_http_client", return_value=client):
            return await WebFetchTool().execute("https://example.invalid/paper")

    async def test_web_fetch_full_text_is_archived_without_silent_truncation(self):
        content = "<html><body>" + "long evidence " * 15000 + "END_MARKER</body></html>"
        result = await self.fetch(httpx.Response(200, text=content, headers={"content-type": "text/html"}))
        self.assertTrue(result["ok"])
        self.assertTrue(result["content"].endswith("END_MARKER"))
        self.assertFalse(result["metadata"]["truncated"])
        with tempfile.TemporaryDirectory() as directory:
            serialize_tool_result(result, artifact_dir=directory)
            saved, = Path(directory).glob("tool-result-*.txt")
            self.assertTrue(saved.read_text(encoding="utf-8").endswith("END_MARKER"))

    async def test_web_binary_oversize_and_http_errors_are_explicit(self):
        result = await self.fetch(httpx.Response(200, content=b"%PDF-", headers={"content-type": "application/pdf"}))
        self.assertEqual(result["metadata"]["code"], "unsupported_media_type")
        with patch("xuloom.tools.web_tool.MAX_FETCH_BYTES", 10):
            result = await self.fetch(httpx.Response(200, text="x" * 11, headers={"content-type": "text/plain"}))
        self.assertEqual(result["metadata"]["code"], "response_too_large")
        result = await self.fetch(httpx.Response(429))
        self.assertEqual(result["metadata"]["http_status"], 429)
        self.assertTrue(result["metadata"]["retryable"])
        result = await self.fetch(httpx.Response(401))
        self.assertFalse(result["metadata"]["retryable"])

    async def test_queue_cancellation_is_not_left_running(self):
        queue = IncrementalWriteQueue()
        entered = asyncio.Event()
        async def work():
            entered.set(); await asyncio.Event().wait()
        task = asyncio.create_task(queue.run("fixture.txt", "edit", work))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(queue.history[0]["status"], "cancelled")

    async def test_waiting_queue_cancellation_and_idle_lock_collection(self):
        import gc
        with tempfile.TemporaryDirectory() as directory:
            queue = IncrementalWriteQueue(lock_directory=Path(directory) / "locks")
            _, lock = queue._lock_for(str(Path(directory) / "file")); await lock.acquire()
            task = asyncio.create_task(queue.run(str(Path(directory) / "file"), "write", lambda: None))
            await asyncio.sleep(.01); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(queue.history[0]["status"], "cancelled")
            lock.release(); del lock; gc.collect()
            self.assertEqual(len(queue._locks), 0)

    async def test_creation_race_at_atomic_publication_never_clobbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"
            original = os.link
            def competing_publish(source, destination):
                Path(destination).write_text("other writer won")
                return original(source, destination)
            tool = FileWriteTool(); tool.context = {}
            with patch("xuloom.core.storage.os.link", competing_publish):
                result = await tool.execute(file_path=str(path), content="must not overwrite")
            self.assertTrue(result["is_error"])
            self.assertEqual(path.read_text(), "other writer won")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    async def test_mutation_queue_waits_for_other_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path, marker = Path(directory) / "file", Path(directory) / "released"
            locks = Path(directory) / "locks"
            identity = hashlib.sha256(os.path.normcase(os.path.realpath(path)).encode()).hexdigest()
            code = ("import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "from xuloom.core.session_lock import SessionLock; lock=SessionLock(sys.argv[2],sys.argv[3]); "
                "lock.acquire(); print('held',flush=True); time.sleep(.3); Path(sys.argv[4]).write_text('done'); lock.close()")
            child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]),
                str(locks), identity, str(marker), stdout=asyncio.subprocess.PIPE)
            try:
                self.assertEqual((await child.stdout.readline()).strip(), b"held")
                queue = IncrementalWriteQueue(lock_directory=locks)
                result = await queue.run(str(path), "write", marker.exists)
                self.assertTrue(result)
                await child.wait()
            finally:
                if child.returncode is None: child.kill(); await child.wait()

    async def test_exited_parent_with_inherited_pipes_has_bounded_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(Path(directory) / "logs"); manager.OUTPUT_DRAIN_TIMEOUT = .05
            code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(.8)']); print('parent done',flush=True)"
            try:
                identity = await manager.start(argv=[sys.executable, "-c", code], cwd=directory, owner="test", timeout_ms=5000)
                result = await asyncio.wait_for(manager.poll(identity, "test", 1000), 2)
                self.assertEqual(result["status"], "output_incomplete")
                self.assertFalse(result["output_complete"])
                self.assertEqual(result["exit_code"], 0)
            finally:
                await manager.close()
                await asyncio.sleep(1)
