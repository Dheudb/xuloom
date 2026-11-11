import tempfile
import unittest
from pathlib import Path

from xuloom.desktop.turns import TurnRecorder
from xuloom.features.file_changes import describe_change, legacy_changes, preview_file
from xuloom.tools.file_write_tool import FileWriteTool
from xuloom.tools.file_edit_tool import FileEditTool


class FileChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_writes_and_edits_report_net_changes_and_failures_do_not_add_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "测试.md")
            recorder = TurnRecorder()
            context = {"capability_event": recorder.accept}
            writer, editor = FileWriteTool(), FileEditTool()
            writer.context = editor.context = context
            await writer.execute(file_path=path, content="a\nb\nc\n")
            await editor.execute(file_path=path, old_string="b\n", new_string="x\ny\n")
            self.assertEqual(len(recorder.record["files"]), 1)
            self.assertEqual((recorder.record["files"][0]["added"], recorder.record["files"][0]["removed"]), (4, 0))
            recorder = TurnRecorder(); context["capability_event"] = recorder.accept
            await editor.execute(file_path=path, old_string="x\ny\n", new_string="z\n")
            result = await editor.execute(file_path=path, old_string="missing", new_string="ignored")
            self.assertTrue(result["is_error"])
            self.assertEqual((recorder.record["files"][0]["added"], recorder.record["files"][0]["removed"]), (1, 2))
            self.assertIn("+z", recorder.record["files"][0]["diff"])

    async def test_unread_overwrite_does_not_report_success_and_empty_new_file_is_clickable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file.txt"; path.write_text("original", encoding="utf-8")
            recorder = TurnRecorder(); tool = FileWriteTool(); tool.context = {"capability_event": recorder.accept}
            result = await tool.execute(file_path=str(path), content="replacement")
            self.assertTrue(result["is_error"]); self.assertFalse(recorder.record["files"])
            await tool.execute(file_path=str(Path(directory) / "empty.txt"), content="")
            self.assertEqual(recorder.record["files"][0]["added"], 0)
            self.assertEqual(preview_file(directory, "empty.txt", [])['content'], "")

    def test_preview_reads_latest_disk_content_and_enforces_workspace_or_actual_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"; project.mkdir()
            path = project / "note.md"; path.write_text("first", encoding="utf-8")
            self.assertEqual(preview_file(project, "note.md", [])['content'], "first")
            path.write_text("latest", encoding="utf-8")
            self.assertEqual(preview_file(project, "note.md", [])['content'], "latest")
            outside = Path(directory) / "outside.md"; outside.write_text("outside", encoding="utf-8")
            with self.assertRaises(ValueError): preview_file(project, "../outside.md", [])
            self.assertEqual(preview_file(project, str(outside), [{"path": str(outside)}])['content'], "outside")
            for value in ["https://example.org/file.md", "file:///etc/passwd", "", "\0"]:
                with self.assertRaises(ValueError): preview_file(project, value, [])
            binary = project / "blob.bin"; binary.write_bytes(b"\0\1\2")
            with self.assertRaises(ValueError): preview_file(project, "blob.bin", [])
            path.unlink()
            with self.assertRaises(ValueError): preview_file(project, "note.md", [])

    def test_legacy_creation_is_counted_and_unknown_overwrite_is_not_invented(self):
        records = legacy_changes([
            {"name": "file_write_tool", "status": "completed", "input": {"file_path": "old.md", "content": "one\ntwo\n"}, "result": "Successfully created file"},
            {"name": "file_edit_tool", "status": "completed", "input": {"file_path": "edit.md", "old_string": "a", "new_string": "b"}, "result": "Successfully edited file"},
            {"name": "file_write_tool", "status": "error", "input": {"file_path": "denied.md", "content": "x"}},
        ], Path.cwd())
        self.assertEqual(len(records), 2); self.assertEqual(records[0]["added"], 2)
        self.assertIsNone(records[1]["added"]); self.assertFalse(records[1]["statsAvailable"])
        self.assertEqual(describe_change("x", "a\r\nb\r\n", "a\nb\n")["added"], 0)

    def test_preview_does_not_follow_replaced_tracked_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"; project.mkdir()
            outside = Path(directory) / "outside.md"; outside.write_text("private", encoding="utf-8")
            alias = project / "alias.md"
            try: alias.symlink_to(outside)
            except OSError: self.skipTest("Symlink creation is unavailable on this host")
            with self.assertRaises(ValueError): preview_file(project, str(alias), [{"path": str(alias)}])
