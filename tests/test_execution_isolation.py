import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xuloom.security.execution import ExecutionSandbox, SandboxUnavailable
from xuloom.utils.command_sessions import CommandSessions


class ExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            policy = ExecutionSandbox(root, "workspace")
            for tool in ("mcp_example", "browser_tool", "lsp_tool", "bash_tool", "task_create_tool", "web_fetch_tool"):
                with self.assertRaises(SandboxUnavailable):
                    policy.check_tool(tool)
            with patch.object(policy, "status", return_value={"available": False, "message": "unavailable"}):
                with self.assertRaisesRegex(SandboxUnavailable, "unavailable"):
                    policy.prepare(argv=[sys.executable, "-c", "print(1)"])
            with self.assertRaises(ValueError):
                ExecutionSandbox(root, "invalid")
            with self.assertRaisesRegex(SandboxUnavailable, "overlaps trusted"):
                ExecutionSandbox(Path(sys.prefix), "workspace").prepare(argv=[sys.executable, "-c", "print(1)"])
            if os.name == "nt":
                with patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(root)/"data")}):
                    for name in ("request-limits", "sandbox-cleanup", "mcp-oauth"):
                        protected = Path(root)/"data"/name
                        protected.mkdir(parents=True)
                        with self.assertRaisesRegex(SandboxUnavailable, "overlaps trusted"):
                            ExecutionSandbox(protected, "workspace").prepare(argv=[sys.executable, "-c", "print(1)"])

    async def test_session_setting_does_not_need_a_model(self):
        from xuloom.desktop.bridge import DesktopBridge
        from xuloom.core.session import SessionManager
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(root) / "data"), "XULOOM_CONFIG_DIR": str(Path(root) / "config")}):
            bridge = DesktopBridge(lambda _: None, root)
            with SessionManager(cwd=root) as manager:
                manager.acquire_writer("fixture")
                self.assertTrue(manager.save_session_state("fixture", [], {}))
            await bridge.dispatch("initialize", {"sessionId": "fixture"})
            mode = "workspace" if ExecutionSandbox(root).status()["available"] else "host"
            await bridge.dispatch("execution.set", {"mode": mode})
            restored = DesktopBridge(lambda _: None, root)
            initial = await restored.dispatch("initialize", {"sessionId": "fixture"})
            self.assertEqual(initial["executionMode"], mode)
            self.assertIsNone(restored.engine)
            if mode == "workspace":
                with self.assertRaisesRegex(ValueError, "MCP"):
                    await restored.dispatch("mcp.test", {"name": "blocked"})
            await bridge.close(); await restored.close()


@unittest.skipUnless(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "Windows x64 LPAC backend")
class NativeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_file_network_environment_and_descendant_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"; workspace.mkdir()
            outside = root / "outside.txt"; outside.write_text("PRIVATE_OUTSIDE", encoding="utf-8")
            protected = root / "readonly-runtime"; protected.mkdir(); (protected / "safe.txt").write_text("SAFE")
            link = workspace / "outside-link"
            result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(root)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            policy = ExecutionSandbox(workspace, "workspace")
            script = """
import json,os,socket,subprocess,sys
from pathlib import Path
root=Path.cwd();outside=Path(sys.argv[1]);protected=Path(sys.argv[2])
out={}
for key,action in {
 'outside_read':lambda:outside.read_text(),
 'outside_write':lambda:outside.write_text('CHANGED'),
 'runtime_write':lambda:(protected/'safe.txt').write_text('CHANGED'),
 'runtime_rename':lambda:protected.rename(root/'renamed'),
 'network':lambda:socket.create_connection(('127.0.0.1',9),timeout=1),
}.items():
 try:action();out[key]='ALLOWED'
 except PermissionError:out[key]='DENIED'
 except OSError as error:out[key]='OS_DENIED:'+str(error.winerror)
if len(sys.argv)>3:
 try:(root/'outside-link/outside.txt').read_text();out['junction']='ALLOWED'
 except PermissionError:out['junction']='DENIED'
(root/'allowed.txt').write_text('OK',encoding='utf-8')
out['secret_present']='XULOOM_SANDBOX_TEST_SECRET' in os.environ
log=(root/'child-output.txt').open('w')
child=subprocess.Popen([sys.executable,'-I','-c',"import time;from pathlib import Path;time.sleep(3);Path('descendant-survived.txt').write_text('BAD')"],stdout=log,stderr=log)
out['child_pid']=child.pid
print(json.dumps(out),flush=True)
"""
            argv = [sys._base_executable, "-I", "-X", "utf8", "-c", script, str(outside), str(protected)] + (["junction"] if result.returncode == 0 else [])
            with patch.dict(os.environ, {"XULOOM_SANDBOX_TEST_SECRET": "DO_NOT_FORWARD"}):
                code, output, error = await policy.communicate(argv=argv, read_roots=[protected], timeout=60)
            self.assertEqual(code, 0, error.decode("utf-8", "replace"))
            report = json.loads(output)
            for key in ("outside_read", "outside_write", "runtime_write", "runtime_rename", "network"):
                self.assertNotEqual(report[key], "ALLOWED", report)
            if result.returncode == 0:
                self.assertEqual(report["junction"], "DENIED")
            self.assertFalse(report["secret_present"])
            self.assertGreater(report["child_pid"], 0)
            self.assertEqual(outside.read_text(), "PRIVATE_OUTSIDE")
            self.assertEqual((protected / "safe.txt").read_text(), "SAFE")
            self.assertEqual((workspace / "allowed.txt").read_text(), "OK")
            await asyncio.sleep(3.5)
            self.assertFalse((workspace / "descendant-survived.txt").exists())

    async def test_native_file_tool_and_owned_command_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"; workspace.mkdir()
            policy = ExecutionSandbox(workspace, "workspace")
            events = []
            result = await policy.tool("file_write_tool", {"file_path": str(workspace / "reply.md"), "content": "你好\n"}, {"capability_event": events.append})
            self.assertTrue(result["ok"], result)
            self.assertEqual((workspace / "reply.md").read_text(encoding="utf-8"), "你好\n")
            self.assertEqual(events[0]["type"], "file_changed")
            manager = CommandSessions(Path(temp) / "outputs")
            manager.execution_sandbox = policy
            try:
                identity = await manager.start(command='echo COMMAND_OK>command.txt', cwd=str(workspace), owner="test", timeout_ms=60000)
                row = await manager.poll(identity, "test", 60000)
                self.assertEqual(row["status"], "completed", manager.read(identity, "test", "stderr"))
                self.assertEqual((workspace / "command.txt").read_text().strip(), "COMMAND_OK")
            finally:
                await manager.close()
