"""Local CLI parity: actual attachments, session checkpoints and loopback media."""
import asyncio
from contextlib import nullcontext
from io import StringIO
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
from rich.console import Console
from tests.test_model_adapters import AdapterFixture
from tests import test_media_generation as media_fixture
from tests import test_noninteractive_cli as cli_fixture
from xuloom.models import model_response
from xuloom.terminal_actions import AttachmentDraft, paths, session_setting, settings_command


class TerminalSettingsTests(AdapterFixture):
    async def test_modes_and_permissions_persist_without_model_calls(self):
        async with self.engine() as engine:
            for mode in ("auto", "ultra", "solo"):
                await session_setting(engine, "mode", mode)
                self.assertEqual(engine.collaboration_mode, mode)
                state = engine.session_manager.load_session_state(engine.session_id)
                self.assertEqual(state["metadata"]["collaboration_mode"], mode)
            engine.permission_manager.always_allow_tools.add("command_tool")
            await session_setting(engine, "permissions", "ask")
            self.assertFalse(engine.permission_manager.always_allow_tools)
            from xuloom.core.storage import history_root
            index = json.loads((history_root(engine.session_cwd) / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["sessions"][engine.session_id]["permissionMode"], "ask")
            self.assertEqual(self.adapter.requests, [])

    async def test_failed_permission_checkpoint_restores_previous_policy_and_memory(self):
        async with self.engine() as engine:
            engine.permission_manager.always_allow_tools.add("command_tool")
            with patch.object(engine, "persist_session_state", return_value=False):
                with self.assertRaises(OSError):
                    await session_setting(engine, "permissions", "ask")
            self.assertEqual(engine.permission_manager.approval_policy.mode, "full")
            self.assertIn("command_tool", engine.permission_manager.always_allow_tools)

    async def test_resume_reads_same_permission_index_as_desktop(self):
        from xuloom.terminal_actions import restore_permissions
        async with self.engine() as engine:
            await session_setting(engine, "permissions", "full")
            engine.permission_manager.approval_policy.set_mode("ask")
            restore_permissions(engine)
            self.assertEqual(engine.permission_manager.approval_policy.mode, "full")
            await session_setting(engine, "permissions", "ask")
            restore_permissions(engine)
            self.assertEqual(engine.permission_manager.approval_policy.mode, "ask")

    async def test_live_worker_blocks_settings_and_menu_cancel_changes_nothing(self):
        async with self.engine() as engine:
            task = asyncio.create_task(asyncio.sleep(30))
            engine.tool_context["subagent_tasks"].tasks["busy"] = task
            try:
                with self.assertRaises(ValueError):
                    await session_setting(engine, "permissions", "ask")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await settings_command(engine, "/mode", NS(choose_menu=lambda **_: ""), Console(file=StringIO()))
            self.assertEqual(engine.collaboration_mode, "solo")
            self.assertEqual(self.adapter.requests, [])

    async def test_isolation_switch_and_failed_save_never_start_a_host_fallback(self):
        async with self.engine() as engine:
            engine.available_tools["fixture_remote"] = NS(mcp_tool_name="remote")
            engine.activated_tools.add("fixture_remote")
            engine.mcp_bridge = NS(cleanup=AsyncMock())
            old_bridge = engine.mcp_bridge
            with patch("xuloom.security.execution.ExecutionSandbox.status", return_value={"available": True}):
                await session_setting(engine, "sandbox", "workspace")
                self.assertTrue(engine.execution_sandbox.enabled)
                old_bridge.cleanup.assert_awaited_once()
                self.assertNotIn("fixture_remote", engine.available_tools)
                with patch.object(engine, "persist_session_state", return_value=False):
                    with self.assertRaises(OSError):
                        await session_setting(engine, "sandbox", "host")
                self.assertTrue(engine.execution_sandbox.enabled)
                with patch.object(engine, "start_protocols", new_callable=AsyncMock) as start:
                    await session_setting(engine, "sandbox", "host")
                    start.assert_awaited_once()
            self.assertEqual(self.adapter.requests, [])


class TerminalAttachmentTests(AdapterFixture):
    async def test_file_and_image_become_visual_input_and_recoverable_history(self):
        file = self.root / "work" / "内容 with spaces.txt"
        file.write_text("selected source", encoding="utf-8")
        image = self.root / "work" / "参考.png"
        Image.new("RGB", (16, 10), "green").save(image)
        draft = AttachmentDraft()
        async with self.engine() as engine:
            draft.add(engine, [file.name, image.name])
            self.assertEqual(len(draft.items), 2)
            self.assertNotEqual(Path(draft.items[0]["path"]), file)
            options = draft.options(engine, "inspect selected sources")
            self.assertEqual(options["input_blocks"][-1]["type"], "image")
            self.assertEqual(self.adapter.requests, [])
            self.adapter.script = [model_response(text="inspected")]
            from xuloom.cli import stream
            result = await stream(engine, "inspect selected sources", [], AsyncMock(), **options)
            self.assertTrue(result["ok"], result)
            draft.consume_saved(engine)
            self.assertFalse(draft.items)
            from xuloom.desktop.turns import conversation_history
            history = conversation_history(engine.messages)
            self.assertEqual(history[0]["text"], "inspect selected sources")
            self.assertEqual(len(history[0]["attachments"]), 2)

    async def test_paths_remove_clear_and_failed_add_keep_pending_files(self):
        self.assertEqual(paths(r'"C:\中文 路径\a.txt" E:\b.png'), [r'C:\中文 路径\a.txt', r'E:\b.png'])
        file = self.root / "work" / "a.txt"; file.write_text("a")
        draft = AttachmentDraft(); console = Console(file=StringIO())
        async with self.engine() as engine:
            draft.command(engine, '/attach "a.txt"', console)
            original = draft.items.copy()
            with self.assertRaises(ValueError):
                draft.add(engine, ["missing.pdf"])
            self.assertEqual(draft.items, original)
            draft.command(engine, "/attach list", console)
            self.assertEqual(draft.items, original)
            draft.command(engine, "/attach remove 1", console)
            self.assertFalse(draft.items)
            draft.add(engine, [file.name])
            draft.command(engine, "/attach clear", console)
            self.assertFalse(draft.items)

    async def test_invalid_image_is_not_published_and_size_limit_checked_before_copy(self):
        file = self.root / "work" / "bad.png"; file.write_bytes(b"not an image")
        async with self.engine() as engine:
            draft = AttachmentDraft()
            with self.assertRaises(OSError):
                draft.add(engine, [file.name])
            self.assertFalse(draft.items)
            with self.assertRaises(ValueError):
                draft.add(engine, [file.name] * 11)
            self.assertEqual(self.adapter.requests, [])


class TerminalMediaTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = media_fixture.MediaTests.asyncSetUp
    asyncTearDown = media_fixture.MediaTests.asyncTearDown

    async def test_explicit_generation_and_query_use_real_tool_receipts_without_chat_model(self):
        from xuloom.media.plugin import run_submit, run_control
        self.scenario = "pending"
        with patch.object(self.engine, "submit_message", side_effect=AssertionError("No chat model allowed")):
            receipt, record = await run_submit(self.engine, {"connection": "media", "model": "image", "prompt": "fixture", "wait_ms": 0})
            self.assertFalse(receipt["is_error"], receipt)
            self.assertIsNotNone(record)
            self.assertEqual(record["phase"], "submitted")
            identity = record["id"]
            self.scenario = "normal"
            receipt, finished = await run_control(self.engine, "status", identity, 2000)
            self.assertFalse(receipt["is_error"], receipt)
            self.assertEqual(finished["phase"], "downloaded")
            self.assertTrue(Path(finished["files"][0]["path"]).is_file())
            self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)

    async def test_approval_denial_never_submits_and_disabled_plugin_does_not_execute(self):
        from xuloom.media.plugin import run_submit
        self.engine.permission_manager.approval_policy.set_mode("ask")
        self.engine.permission_manager.prompt_handler = AsyncMock(return_value="deny")
        receipt, record = await run_submit(self.engine, {"connection": "media", "model": "image", "prompt": "fixture"})
        self.assertTrue(receipt["is_error"])
        self.assertIsNone(record)
        self.assertEqual(self.requests, [])
        self.engine.available_tools.pop("media_tool", None)
        self.engine.latent_tools.pop("media_tool", None)
        with self.assertRaises(ValueError):
            await run_submit(self.engine, {"connection": "media", "model": "image", "prompt": "fixture"})
        self.assertEqual(self.requests, [])

    async def test_media_picker_cancel_and_listing_never_make_provider_requests_or_reveal_key(self):
        from xuloom.media.terminal import command
        output = StringIO(); console = Console(file=output)
        console.input = Mock(side_effect=AssertionError("Do not ask for credentials to list models"))
        tui = NS(choose_menu=Mock(return_value=""), suspend_streaming=nullcontext)
        await command(self.engine, "/media", tui, console)
        await command(self.engine, "/media list", tui, console)
        await command(self.engine, "/media generate", tui, console)
        self.assertIn("fal-ai/app/image", output.getvalue())
        self.assertNotIn("MEDIA_ONLY_SECRET", output.getvalue())
        self.assertEqual(self.requests, [])

    async def test_cli_generate_reuses_shared_model_and_approval_result(self):
        from xuloom.cli import parser
        from xuloom.media.cli import dispatch
        args = parser().parse_args(["media", "generate", "--connection", "media", "--model", "image", "--prompt", "fixture", "--wait-ms", "0"])
        with patch("xuloom.cli.open_engine", AsyncMock(return_value=self.engine)), patch.object(self.engine, "aclose", new_callable=AsyncMock):
            result = await dispatch(args, AsyncMock())
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["generation_completed"])
        self.assertEqual(result["job"]["model_id"], "image")
        self.assertNotIn("MEDIA_ONLY_SECRET", json.dumps(result))

    async def test_unknown_submission_is_reported_and_not_automatically_submitted_again(self):
        from xuloom.media.plugin import run_submit
        self.scenario = "lost"
        values = {"connection": "media", "model": "image", "prompt": "unknown fixture", "wait_ms": 0}
        _, record = await run_submit(self.engine, values)
        self.assertEqual(record["phase"], "unknown")
        _, repeated = await run_submit(self.engine, values)
        self.assertEqual(repeated["id"], record["id"])
        self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)

    async def test_edit_preserves_sibling_models_and_cancel_does_not_write(self):
        from xuloom.media.terminal import command
        from xuloom.media import config
        old = config.public_models()
        console = Console(file=StringIO())
        console.input = Mock(side_effect=["", "changed-image"])
        tui = NS(choose_menu=Mock(side_effect=["image", "0", ""]))
        with patch("xuloom.media.terminal.getpass", return_value=""):
            await command(self.engine, "/media edit", tui, console)
        self.assertEqual(config.public_models(), old)
        console.input.side_effect = ["", "changed-image"]
        tui.choose_menu.side_effect = ["image", "0", "yes"]
        with patch("xuloom.media.terminal.getpass", return_value=""):
            await command(self.engine, "/media edit", tui, console)
        rows = config.public_models()
        self.assertEqual(len(rows), len(old))
        self.assertTrue(any(row["model"] == "changed-image" for row in rows))
        self.assertEqual([r for r in old if r["id"] != "image"], [r for r in rows if r["id"] != "image"])
        self.assertEqual(self.requests, [])

    async def test_actual_media_cli_works_without_a_chat_model_and_requires_explicit_approval(self):
        from xuloom.media import config
        folder = self.root / "cli-config"; folder.mkdir()
        (folder / "config.json").write_text(json.dumps({"media_connections": config.load_config()["media_connections"]}), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XULOOM_", "OPENAI_", "ANTHROPIC_"))}
        env.update(XULOOM_CONFIG_DIR=str(folder), XULOOM_DATA_DIR=str(self.root / "cli-data"), HOME=str(self.root / "home"),
                   USERPROFILE=str(self.root / "home"), XULOOM_NETWORK_MODE="direct", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        argv = [sys.executable, "-m", "xuloom", "media", "generate", "--cwd", str(self.root), "--connection", "media", "--model", "image",
                "--prompt", "fixture cli", "--wait-ms", "0", "--json"]
        process = await asyncio.to_thread(subprocess.run, argv, env=env, text=True, encoding="utf-8", capture_output=True, timeout=35)
        result = json.loads(process.stdout.splitlines()[-1])["result"]
        self.assertEqual(result["reason"], "approval_required", process.stdout + process.stderr)
        self.assertEqual(self.requests, [])
        process = await asyncio.to_thread(subprocess.run, argv + ["--permission", "full"], env=env, text=True,
                                         encoding="utf-8", capture_output=True, timeout=35)
        result = json.loads(process.stdout.splitlines()[-1])["result"]
        self.assertTrue(result["ok"], process.stdout + process.stderr)
        self.assertTrue(result["generation_completed"])
        self.assertTrue(result["session_id"])
        self.assertNotIn("MEDIA_ONLY_SECRET", process.stdout + process.stderr)


class TerminalProcessTests(unittest.TestCase):
    setUp = cli_fixture.CliTests.setUp
    tearDown = cli_fixture.CliTests.tearDown
    cli = cli_fixture.CliTests.cli

    def test_exec_attachment_reaches_model_and_desktop_history_and_resume_cannot_replace_it(self):
        file = self.root / "附件 sample.txt"; file.write_text("source", encoding="utf-8")
        process, _, result = self.cli("exec", "inspect source", "--attach", str(file))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(result["ok"])
        self.assertIn("user-attachments", json.dumps(self.requests))
        sid = result["session_id"]
        process, _, result = self.cli("exec", "--resume", "--session", sid, "--attach", str(file))
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(len(self.requests), 1)

    def test_actual_interactive_settings_attachment_menu_and_media_list_do_not_call_model(self):
        file = self.root / "source file.txt"; file.write_text("source")
        commands = '\n'.join(["/mode ultra", "/mode status", "/permissions full", "/permissions status", "/permissions ask",
            '/attach "source file.txt"', "/attach list", "/attach remove 1", "/media list", "/quit", ""])
        process = subprocess.run([sys.executable, "-m", "xuloom"], input=commands, text=True, encoding="utf-8",
            cwd=self.root, env=self.env, capture_output=True, timeout=40)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("Critical Error", process.stdout)
        self.assertIn("mode → ultra", process.stdout)
        self.assertIn("source file.txt", process.stdout)
        self.assertIn("models", process.stdout)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
