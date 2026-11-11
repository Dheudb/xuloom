"""Real requests and hook processes must use the instance workspace, not process globals."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

from xuloom.core.engine import QueryEngine
from xuloom.core.storage import atomic_json
from xuloom.skills.registry import SkillRegistry
if __package__:
    from . import test_tool_recovery as recovery
else:
    import test_tool_recovery as recovery


class WorkspaceBindingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    async def recreate(self):
        await self.engine.aclose()
        self.engine = QueryEngine(model="fixture", model_provider="openai", cwd=str(self.root))

    async def test_actual_model_request_has_correct_directory_and_no_implicit_legacy_instructions(self):
        foreign = self.root / "foreign"; foreign.mkdir()
        (foreign / "AGENTS.md").write_text("FOREIGN_CONTEXT_MARKER", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("SELECTED_WORKSPACE_MARKER", encoding="utf-8")
        home = self.root / "home"; (home / ".claude").mkdir(parents=True)
        (home / ".claude/CLAUDE.md").write_text("IMPLICIT_LEGACY_MARKER", encoding="utf-8")
        with patch("os.getcwd", return_value=str(foreign)):
            await self.recreate(); self.fixture(["answer"])
            result = await self.engine.run("你好", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok)
        prompt = json.dumps(self.requests[0]["messages"], ensure_ascii=False)
        self.assertIn("SELECTED_WORKSPACE_MARKER", prompt)
        self.assertNotIn("FOREIGN_CONTEXT_MARKER", prompt)
        self.assertNotIn("IMPLICIT_LEGACY_MARKER", prompt)
        self.assertIn(str(self.root).replace("\\", "\\\\"), prompt)
        self.assertEqual(self.engine.context_builder.cwd, str(self.root))

    async def test_legacy_opt_in_has_one_instruction_loader(self):
        home = self.root / "home"; (home / ".claude").mkdir(parents=True)
        (home / ".claude/CLAUDE.md").write_text("OPT_IN_LEGACY_ONCE", encoding="utf-8")
        SkillRegistry(self.root).update(legacy_instructions=True)
        self.fixture(["answer"]); await self.engine.run("你好", sys_print_callback=lambda _: None)
        prompt = json.dumps(self.requests[0]["messages"], ensure_ascii=False)
        self.assertEqual(prompt.count("OPT_IN_LEGACY_ONCE"), 1, "Instructions must not also enter through the memory loader")

    async def test_project_auto_memory_uses_selected_workspace_after_refresh(self):
        foreign = self.root / "foreign"; foreign.mkdir()
        for directory, label in ((self.root, "RIGHT_MEMORY"), (foreign, "WRONG_MEMORY")):
            key = hashlib.sha256(str(directory.absolute()).encode()).hexdigest()[:16]
            target = self.root / "data/memory/projects" / key / "MEMORY.md"
            target.parent.mkdir(parents=True); target.write_text(label, encoding="utf-8")
        with patch("os.getcwd", return_value=str(foreign)):
            self.engine.refresh_memory_files()
        summary = self.engine.get_memory_summary()
        self.assertIn("RIGHT_MEMORY", summary); self.assertNotIn("WRONG_MEMORY", summary)

    async def test_hook_config_and_real_subprocess_bind_to_selected_workspace(self):
        foreign = self.root / "foreign"; foreign.mkdir()
        atomic_json(self.root / ".xuloom/hooks.json", {"SessionStart": [{"content": "SELECTED_HOOK"}]})
        atomic_json(foreign / ".xuloom/hooks.json", {"SessionStart": [{"content": "FOREIGN_HOOK"}]})
        script = self.root / "show_context.py"
        script.write_text("import os,json;print(json.dumps({'cwd':os.getcwd(),'project':os.environ['XULOOM_PROJECT_DIR']}))", encoding="utf-8")
        command = subprocess.list2cmdline([sys.executable, str(script)]) if os.name == "nt" else shlex.join([sys.executable, str(script)])
        with patch("os.getcwd", return_value=str(foreign)):
            await self.recreate()
            values = self.engine.hook_manager.execute("SessionStart", {})
            report = await self.engine.hook_manager._exec_command_hook({"command": command}, {})
        self.assertEqual(values, ["SELECTED_HOOK"])
        self.assertEqual(report.exit_code, 0, report.stderr)
        self.assertEqual(Path(report.json_output["cwd"]), self.root)
        self.assertEqual(Path(report.json_output["project"]), self.root)

    async def test_workspace_copy_resolves_instance_root_without_changing_process_cwd(self):
        foreign = self.root / "foreign"; foreign.mkdir()
        with patch("os.getcwd", return_value=str(foreign)):
            await self.recreate()
            directory, is_git = self.engine.sandbox_manager._get_workspace_root()
        self.assertEqual(Path(directory), self.root)
        self.assertFalse(is_git)

    async def test_relative_file_write_and_permission_summary_use_instance_workspace(self):
        foreign = self.root / "foreign"; foreign.mkdir()
        requests = []
        async def approve(request): requests.append(request); return "allow"
        self.engine.permission_manager.set_prompt_handler(approve)
        with patch("os.getcwd", return_value=str(foreign)):
            receipt = await self.engine._execute_tool_wrapper({"id": "relative-write", "name": "file_write_tool",
                "input": {"file_path": "selected.txt", "content": "ONLY_IN_SELECTED_WORKSPACE"}}, lambda _: None)
        self.assertFalse(receipt["is_error"], receipt)
        self.assertTrue((self.root / "selected.txt").is_file())
        self.assertFalse((foreign / "selected.txt").exists())
        self.assertEqual(requests[0].input_payload["file_path"], str(self.root / "selected.txt"))

    async def test_shell_without_optional_arguments_uses_instance_directory(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        foreign = self.root / "foreign"; foreign.mkdir()
        with patch("os.getcwd", return_value=str(foreign)):
            result = await self.engine._execute_tool_wrapper({"id": "shell-no-optionals", "name": "bash_tool",
                "input": {"command": "echo LOCAL_COMMAND_PROOF > command-proof.txt"}}, lambda _: None)
        self.assertFalse(result["is_error"], result)
        self.assertIn("LOCAL_COMMAND_PROOF", (self.root / "command-proof.txt").read_text())
        self.assertFalse((foreign / "command-proof.txt").exists())

    async def test_workspace_copy_paths_do_not_redirect_a_similarly_named_sibling(self):
        manager = self.engine.sandbox_manager
        clone = self.root / "copy"; clone.mkdir()
        manager.active_sandbox = {"root": str(self.root), "path": str(clone), "branch": "fixture", "mode": "filesystem_clone"}
        sibling = self.root.with_name(self.root.name + "-sibling") / "keep.txt"
        self.assertEqual(Path(manager.resolve_path(str(sibling))), sibling)
        self.assertEqual(Path(manager.resolve_path("nested/file.txt")), clone / "nested/file.txt")
        self.assertEqual(Path(manager.resolve_path(str(self.root / "file.txt"))), clone / "file.txt")
        self.assertEqual(Path(manager.resolve_path(str(clone / "file.txt"))), clone / "file.txt")

    async def test_workspace_copy_write_authorizes_the_actual_destination(self):
        clone = self.root / "copy"; clone.mkdir(); requests = []
        self.engine.sandbox_manager.active_sandbox = {"root": str(self.root), "path": str(clone), "branch": "fixture", "mode": "filesystem_clone"}
        async def approve(request): requests.append(request); return "allow"
        self.engine.permission_manager.set_prompt_handler(approve)
        receipt = await self.engine._execute_tool_wrapper({"id": "copy-write", "name": "file_write_tool",
            "input": {"file_path": str(self.root / "new.txt"), "content": "CLONE_ONLY"}}, lambda _: None)
        self.assertFalse(receipt["is_error"], receipt)
        self.assertEqual((clone / "new.txt").read_text(), "CLONE_ONLY")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(requests[0].input_payload["file_path"], str(clone / "new.txt"))

    async def test_search_default_is_instance_workspace_and_remote_tool_paths_are_not_guessed(self):
        from pydantic import BaseModel
        from xuloom.tools.base import BaseAgenticTool
        foreign = self.root / "foreign"; foreign.mkdir()
        (foreign / "wrong-marker.txt").write_text("foreign", encoding="utf-8")
        (self.root / "right-marker.txt").write_text("right", encoding="utf-8")
        with patch("os.getcwd", return_value=str(foreign)):
            result = await self.engine.available_tools["glob_tool"](pattern="*-marker.txt")
        self.assertIn("right-marker.txt", result)
        class RemoteInput(BaseModel):
            path: str
        class RemoteTool(BaseAgenticTool):
            input_schema = RemoteInput
            async def execute(self, path): return path
        remote = RemoteTool(); remote.context = self.engine.tool_context
        self.engine.sandbox_manager.active_sandbox = {"root": str(self.root), "path": str(self.root / "copy"), "branch": "fixture"}
        self.assertEqual(await remote(path="provider-side/document"), "provider-side/document")
