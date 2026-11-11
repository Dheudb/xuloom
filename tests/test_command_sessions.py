import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from xuloom.utils.command_sessions import CommandSessions
from xuloom.tools.command_tool import CommandTool, CommandInput
from xuloom.security.permissions import ApprovalPolicy, PermissionManager


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = CommandSessions(Path(self.temp.name) / "logs")
        self.tool = CommandTool()
        self.tool.context = {"command_sessions": self.manager, "agent_id": "owner", "session_cwd": self.temp.name}

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def start(self, code, **kwargs):
        return json.loads(await self.tool.execute(argv=[sys.executable, "-u", "-c", code], wait_ms=1000, **kwargs))

    async def test_command_schema_rejects_subagent_namespace_and_preserves_command_ids(self):
        from pydantic import ValidationError
        with self.assertRaisesRegex(ValidationError, 'agent_tool'):
            CommandInput.model_validate({'action':'poll','command_id':'subagent-00000000-0000-0000-0000-000000000000'})
        value = CommandInput.model_validate({'action':'poll','command_id':'cmd-'+'a'*32})
        self.assertEqual(value.command_id, 'cmd-'+'a'*32)
        self.assertEqual(self.manager.records, {})

    async def test_exact_utf8_stdin_and_separate_json_stderr(self):
        text = '你好\n"quotes" $HOME `not shell`\n' * 1000
        result = await self.start("import sys,json; text=sys.stdin.read(); print(json.dumps({'text':text},ensure_ascii=False)); print('diagnostic',file=sys.stderr)", stdin=text)
        identity = result["id"]
        chunks, offset = [], 0
        while True:
            page = self.manager.read(identity, "owner", offset=offset, max_bytes=401)
            chunks.append(page["text"])
            offset = page["next_offset"]
            if page["eof"]:
                break
        self.assertEqual(json.loads("".join(chunks))["text"], text)
        self.assertEqual(self.manager.read(identity, "owner", "stderr")["text"].strip(), "diagnostic")
        self.assertEqual(result["cwd"], str(Path(self.temp.name).resolve()))

    async def test_interactive_pipe_and_eof_without_shell_wrapper(self):
        result = await self.start("import sys; print('READY',flush=True); line=sys.stdin.readline(); print('GOT:'+line.strip(),flush=True); print('END:'+sys.stdin.read(),flush=True)", keep_stdin_open=True)
        self.assertEqual(result["status"], "running")
        result = json.loads(await self.tool.execute(action="write", command_id=result["id"], stdin="中文\n后续", eof=True))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"]["text"], os.linesep.join(["READY", "GOT:中文", "END:后续", ""]))
        with self.assertRaises(ValueError):
            await self.manager.write(result["id"], "owner", "late")

    async def test_large_output_survives_restart_and_owner_isolation(self):
        result = await self.start("import sys; sys.stdout.write('中文abc'*30000)")
        self.assertEqual(result["status"], "completed")
        restored = CommandSessions(self.manager.root)
        restored.restore()
        with self.assertRaises(ValueError):
            restored.read(result["id"], "other")
        text, offset = "", 0
        while True:
            page = restored.read(result["id"], "owner", offset=offset, max_bytes=4097)
            text += page["text"]
            offset = page["next_offset"]
            if page["eof"]:
                break
        self.assertEqual(text, "中文abc" * 30000)

    async def test_bounded_poll_timeout_cancel_and_restored_liveness(self):
        result = await self.start("import time; time.sleep(30)", timeout_ms=150)
        final = await self.manager.poll(result["id"], "owner", 3000)
        self.assertEqual(final["status"], "timed_out")
        self.assertIsNotNone(final["exit_code"])
        result = await self.start("import time; time.sleep(30)")
        restored = CommandSessions(self.manager.root)
        restored.restore()
        self.assertEqual(restored.records[result["id"]]["status"], "interrupted")
        final = await self.manager.cancel(result["id"], "owner")
        self.assertEqual(final["status"], "cancelled")

    async def test_plan_and_ask_modes_cannot_start_or_write_without_approval(self):
        denied = PermissionManager(mode_getter=lambda: "plan", approval_policy=ApprovalPolicy("full"))
        self.assertEqual((await denied.authorize(self.tool, {"action": "start"})).behavior, "deny")
        ask = PermissionManager()
        self.assertEqual((await ask.authorize(self.tool, {"action": "write"})).behavior, "deny")
        self.assertEqual((await ask.authorize(self.tool, {"action": "read"})).behavior, "allow")
        capabilities = json.loads(await self.tool.execute(action="capabilities"))
        self.assertFalse(capabilities["pty"])
        self.assertFalse(self.tool.is_concurrency_safe_call(action="start"))
        self.assertFalse(self.tool.is_concurrency_safe_call(action="write"))
        self.assertTrue(self.tool.is_concurrency_safe_call(action="read"))

    async def test_output_limit_is_explicit_and_process_is_stopped(self):
        self.manager.OUTPUT_LIMIT = 1024
        result = await self.start("import sys; sys.stdout.write('x'*10000); sys.stdout.flush()")
        final = await self.manager.poll(result["id"], "owner", 3000)
        self.assertEqual(final["status"], "output_limit")
        self.assertIsNotNone(final["exit_code"])

    async def test_wait_up_to_120_seconds_returns_early_and_can_be_cancelled(self):
        result = json.loads(await self.tool(argv=[sys.executable, "-c", "print('WAIT_LIMIT_PASS')"], wait_ms=120000))
        self.assertEqual(result["status"], "completed")
        self.assertIn("WAIT_LIMIT_PASS", result["output"]["text"])
        self.assertEqual(CommandInput(wait_ms=15000).wait_ms, 15000)
        self.assertEqual(json.loads(await self.tool(action="capabilities"))["max_wait_ms"], 120000)
        for invalid in (-1, 120001):
            with self.assertRaises(ValueError):
                CommandInput(wait_ms=invalid)
        running = json.loads(await self.tool(argv=[sys.executable, "-c", "import sys; sys.stdin.read()"],
            keep_stdin_open=True, wait_ms=0))
        poll = asyncio.create_task(self.tool(action="poll", command_id=running["id"], wait_ms=120000))
        await asyncio.sleep(0)
        poll.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await poll
        self.assertEqual(self.manager.records[running["id"]]["status"], "running")
        self.assertEqual((await self.manager.cancel(running["id"], "owner"))["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
