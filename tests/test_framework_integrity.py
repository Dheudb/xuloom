"""Production tool contexts and failure boundaries, without remote models."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from xuloom.context.file_state_cache import FileStateCache
from xuloom.context.tool_results import serialize_tool_result
from xuloom.features.artifact_tracker import ArtifactTracker
from xuloom.features.incremental_write_queue import IncrementalWriteQueue
from xuloom.features.verification import VerificationManager
from xuloom.tools.bash_tool import BashTool
from xuloom.tools.file_write_tool import FileWriteTool
from xuloom.tools.file_edit_tool import FileEditTool
from xuloom.tools.notebook_edit_tool import NotebookEditTool
from xuloom.utils.command_sessions import CommandSessions
from xuloom.utils.shell import decode_output, shell_guidance


def command(code):
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


class IntegrityTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return {"file_state_cache": FileStateCache(), "artifact_tracker": ArtifactTracker(),
                "verification_manager": VerificationManager(), "incremental_write_queue": IncrementalWriteQueue()}

    async def test_actual_write_edit_notebook_context(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.context()
            writer, editor, notebook = FileWriteTool(), FileEditTool(), NotebookEditTool()
            writer.context = editor.context = notebook.context = context
            path = str(Path(directory) / "sample.html")
            result = await writer.execute(file_path=path, content="first\n")
            self.assertTrue(result["ok"])
            result = await editor.execute(file_path=path, old_string="first", new_string="second")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(path).read_text(), "second\n")
            self.assertEqual(context["verification_manager"].modified_files[path]["count"], 2)
            self.assertFalse(context["verification_manager"].should_verify())
            path = str(Path(directory) / "book.ipynb")
            await writer.execute(file_path=path, content=json.dumps({"cells": [{"id": "a", "cell_type": "markdown", "source": ["before"]}], "nbformat": 4, "metadata": {}}))
            result = await notebook.execute(notebook_path=path, cell_id="a", new_source="after")
            self.assertFalse(isinstance(result, dict) and result.get("is_error"))
            self.assertEqual(json.loads(Path(path).read_text())["cells"][0]["source"], "after")

    async def test_observer_failure_does_not_invalidate_written_file(self):
        for observer, method in [("file_state_cache", "record_write"), ("artifact_tracker", "record_file_change"), ("verification_manager", "record_file_modification")]:
            with self.subTest(observer=observer), tempfile.TemporaryDirectory() as directory:
                context = self.context()
                writer = FileWriteTool(); writer.context = context
                path = str(Path(directory) / "kept.txt")
                with patch.object(context[observer], method, side_effect=RuntimeError("injected observer failure")):
                    result = await writer.execute(file_path=path, content="kept")
                self.assertTrue(result["ok"])
                self.assertTrue(result["metadata"]["write_applied"])
                self.assertEqual(result["metadata"]["post_write_warnings"][0]["observer"], observer)
                self.assertEqual(Path(path).read_text(), "kept")

    async def test_notebook_write_failure_has_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.ipynb"
            path.write_text(json.dumps({"cells": [{"id": "a", "cell_type": "markdown", "source": ["before"]}]}))
            before = path.read_bytes()
            tool = NotebookEditTool(); tool.context = {"read_file_state": {str(path): True}}
            with patch("xuloom.core.storage.os.replace", side_effect=OSError("injected write denial")):
                result = await tool.execute(notebook_path=str(path), cell_id="a", new_source="after")
            self.assertTrue(result["is_error"])
            self.assertEqual(path.read_bytes(), before)

    async def test_shell_keeps_full_output_and_explicit_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = BashTool(); tool.context = {"session_cwd": directory}
            result = await tool.execute(command=command("import os; print(os.getcwd()); print('x'*45000+'END_MARKER')"))
            self.assertTrue(result["ok"])
            self.assertEqual(os.path.normcase(directory), os.path.normcase(result["content"].splitlines()[0]))
            self.assertTrue(result["content"].rstrip().endswith("END_MARKER"))
            rendered = serialize_tool_result(result, artifact_dir=directory)
            archived = list(Path(directory).glob("tool-result-*.txt"))
            self.assertEqual(len(archived), 1)
            self.assertIn("Reference path", rendered)
            self.assertTrue(archived[0].read_text(encoding="utf-8").rstrip().endswith("END_MARKER"))

    async def test_cancelled_shell_cannot_leave_delayed_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "late.txt"
            tool = BashTool(); tool.context = {"session_cwd": directory}
            # Gate the side effect so a busy event loop cannot let the old
            # .8-second delay expire before the cancellation timer runs.
            code = "import time; from pathlib import Path; Path('ready').write_text('ready'); exec(\"while not Path('release').exists(): time.sleep(.02)\"); Path('late.txt').write_text('bad')"
            work = asyncio.create_task(tool.execute(command=command(code)))
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
            self.assertFalse(target.exists())

    async def test_command_output_limit_terminates_without_pipe_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(directory); manager.OUTPUT_LIMIT = 1000
            try:
                identity = await manager.start(argv=[sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000); sys.stdout.flush()"], cwd=directory, owner="test")
                result = await asyncio.wait_for(manager.poll(identity, "test", 5000), 8)
                self.assertEqual(result["status"], "output_limit")
                self.assertIsNotNone(result["exit_code"])
            finally: await manager.close()

    async def test_command_output_storage_failure_is_terminal_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(directory)
            original = Path.open
            def checked_open(path, *args, **kwargs):
                if path.suffix == ".stdout": raise OSError("injected disk failure")
                return original(path, *args, **kwargs)
            try:
                with patch.object(Path, "open", checked_open):
                    identity = await manager.start(argv=[sys.executable, "-c", "print('output')"], cwd=directory, owner="test")
                    result = await asyncio.wait_for(manager.poll(identity, "test", 5000), 8)
                self.assertEqual(result["status"], "storage_error")
                self.assertIn("disk failure", result["output_error"])
            finally: await manager.close()

    def test_windows_shell_description_and_legacy_output(self):
        self.assertEqual(decode_output("中文".encode()), "中文")
        if os.name == "nt":
            self.assertIn("cmd.exe", shell_guidance())
            with patch("ctypes.windll.kernel32.GetConsoleOutputCP", return_value=936):
                self.assertEqual(decode_output("目录已经存在".encode("gbk")), "目录已经存在")

    async def test_initial_command_receipt_failure_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(directory)
            try:
                with patch("xuloom.core.storage.atomic_json", side_effect=OSError("receipt unavailable")):
                    with self.assertRaises(OSError):
                        await manager.start(argv=[sys.executable, "-c", "import time; time.sleep(30)"], cwd=directory, owner="test")
                for row in manager.records.values():
                    self.assertIsNotNone(row["process"].returncode)
                    self.assertEqual(row["status"], "storage_error")
            finally: await manager.close()

    async def test_final_command_receipt_failure_never_reports_completed(self):
        from xuloom.core.storage import atomic_json
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(directory)
            calls = 0
            def persist(*args):
                nonlocal calls
                calls += 1
                if calls > 1: raise OSError("final receipt unavailable")
                return atomic_json(*args)
            try:
                with patch("xuloom.core.storage.atomic_json", persist):
                    identity = await manager.start(argv=[sys.executable, "-c", "import time; time.sleep(.1)"], cwd=directory, owner="test")
                    result = await manager.poll(identity, "test", 5000)
                self.assertEqual(result["status"], "storage_error")
                self.assertIn("final receipt", result["persistence_error"])
            finally: await manager.close()

    async def test_workspace_routing_preserves_command_control_json(self):
        from xuloom.tools.command_tool import CommandTool
        with tempfile.TemporaryDirectory() as directory:
            manager = CommandSessions(directory)
            tool = CommandTool()
            tool.context = {"command_sessions": manager, "agent_id": "owner", "session_cwd": directory,
                "sandbox_manager": SimpleNamespace(active_sandbox={"branch": "fixture", "path": directory},
                    resolve_working_directory=lambda value: directory)}
            try:
                result = json.loads(await tool(action="start", cwd=directory,
                    argv=[sys.executable, "-c", "print('ok')"], wait_ms=1000))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["output"]["text"].strip(), "ok")
            finally: await manager.close()
