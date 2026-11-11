import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.security import execution
from xuloom.security.execution import ExecutionSandbox, SandboxUnavailable, bwrap_argv, seatbelt_profile


class LaunchConstructionTests(unittest.TestCase):
    def test_bwrap_mounts_only_system_runtime_and_workspace(self):
        argv = bwrap_argv(["python3", "-c", "print(1)"], workspace="/work/project", cwd="/work/project/src",
                          read_roots=["/home/me/.venv"], timeout_ms=90000, bwrap="/usr/bin/bwrap",
                          timeout_program="/usr/bin/timeout")
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        for flag in ("--die-with-parent", "--new-session", "--unshare-all"):
            self.assertIn(flag, argv)
        pairs = [tuple(argv[i:i + 3]) for i in range(len(argv) - 2)]
        self.assertIn(("--bind", "/work/project", "/work/project"), pairs)
        self.assertIn(("--ro-bind", "/home/me/.venv", "/home/me/.venv"), pairs)
        self.assertIn(("--ro-bind-try", "/usr", "/usr"), pairs)
        writable = [argv[i + 1] for i, item in enumerate(argv) if item == "--bind"]
        self.assertEqual(writable, ["/work/project"])  # nothing else is writable
        self.assertNotIn("/home/me", [argv[i + 1] for i, item in enumerate(argv) if item.endswith("bind")])
        separator = argv.index("--")
        self.assertEqual(argv[argv.index("--chdir") + 1], "/work/project/src")
        self.assertEqual(argv[separator + 1:separator + 3], ["/usr/bin/env", "-i"])
        self.assertEqual(argv[-6:], ["/usr/bin/timeout", "--kill-after=5", "90s", "python3", "-c", "print(1)"])

    def test_tool_processes_never_inherit_credentials(self):
        from xuloom.security.execution import restricted_posix_environment
        environment = restricted_posix_environment("/tmp/x", {"PATH": "/usr/bin", "LANG": "C.UTF-8",
                                                               "OPENAI_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "s"})
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["HOME"], "/tmp/x")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-secret"}):
            argv = bwrap_argv(["true"], workspace="/w", cwd="/w", read_roots=[], timeout_ms=0)
        self.assertFalse(any("sk-secret" in item for item in argv))

    def test_seatbelt_denies_by_default_and_quotes_paths(self):
        profile = seatbelt_profile(workspace='/Users/me/work "x"', read_roots=["/Users/me/.venv"], temporary="/private/tmp/x")
        lines = profile.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        self.assertNotIn("network", profile)  # no allow rule for networking
        write = next(line for line in lines if line.startswith("(allow file-write*"))
        self.assertIn('(subpath "/Users/me/work \\"x\\"")', write)
        self.assertIn('(subpath "/private/tmp/x")', write)
        self.assertNotIn(".venv", write)
        read = next(line for line in lines if line.startswith("(allow file-read*"))
        self.assertIn('(subpath "/Users/me/.venv")', read)
        self.assertNotIn('(subpath "/Users")', read)


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "work"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(self.temp.name) / "data"),
                                           "HOME": str(Path(self.temp.name) / "home"),
                                           "USERPROFILE": str(Path(self.temp.name) / "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_backend_fails_closed_with_guidance(self):
        sandbox = ExecutionSandbox(self.workspace, "workspace")
        with patch.object(execution, "detect_backend", return_value=None), patch.object(sys, "platform", "linux"):
            status = sandbox.status()
            self.assertFalse(status["available"])
            self.assertIn("bubblewrap", status["message"])
            with self.assertRaises(SandboxUnavailable):
                sandbox.prepare(command="echo hi")

    def test_linux_prepare_builds_a_confined_launch(self):
        sandbox = ExecutionSandbox(self.workspace, "workspace")
        which = {"bwrap": "/usr/bin/bwrap", "timeout": "/usr/bin/timeout"}
        with patch.object(execution, "detect_backend", return_value="linux-bwrap"), \
                patch.object(execution.shutil, "which", side_effect=lambda name: which.get(name)):
            self.assertTrue(sandbox.status()["experimental"])
            launch = sandbox.prepare(command="echo hi > out.txt", timeout_ms=5000)
        argv = launch.argv
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "echo hi > out.txt"])
        self.assertEqual([argv[i + 1] for i, item in enumerate(argv) if item == "--bind"], [str(self.workspace.resolve())])
        home = str(Path(os.environ["HOME"]).resolve())
        self.assertFalse(any(item == home or item.startswith(home + os.sep) for item in argv))
        launch.close()

    def test_macos_prepare_uses_a_profile_and_private_temp(self):
        sandbox = ExecutionSandbox(self.workspace, "workspace")
        with patch.object(execution, "detect_backend", return_value="macos-seatbelt"), \
                patch.object(execution.shutil, "which", side_effect=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None):
            launch = sandbox.prepare(command="ls")
        try:
            self.assertEqual(launch.argv[:2], ["/usr/bin/sandbox-exec", "-p"])
            self.assertIn("(deny default)", launch.argv[2])
            self.assertTrue(Path(launch.directory.name).is_dir())
            self.assertEqual(launch.argv[-3:], ["/bin/sh", "-c", "ls"])
            self.assertIn("-i", launch.argv[launch.argv.index("/usr/bin/env"):][:2])
        finally:
            launch.close()

    def test_workspace_overlapping_the_runtime_is_refused_on_every_backend(self):
        package = Path(execution.__file__).resolve().parents[1]
        for backend in ("linux-bwrap", "macos-seatbelt"):
            sandbox = ExecutionSandbox(package, "workspace")
            with patch.object(execution, "detect_backend", return_value=backend):
                with self.assertRaisesRegex(SandboxUnavailable, "overlaps"):
                    sandbox.prepare(command="true")


def _bwrap_works():
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    probe = subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True)
    return probe.returncode == 0


@unittest.skipUnless(_bwrap_works(), "needs Linux with a working bubblewrap")
class LinuxConfinementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "work"
        self.workspace.mkdir()
        self.secret = Path(self.temp.name) / "outside.txt"
        self.secret.write_text("secret", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def run_in_sandbox(self, command):
        sandbox = ExecutionSandbox(self.workspace, "workspace")
        return asyncio.run(sandbox.communicate(command=command, timeout=60))

    def test_workspace_is_writable_and_nothing_else_is_visible(self):
        code, output, error = self.run_in_sandbox(
            f"echo ok > inside.txt && cat inside.txt && (cat {self.secret} || echo hidden) && "
            f"(touch {Path(self.temp.name) / 'escape.txt'} || echo readonly)")
        self.assertEqual(code, 0, error)
        self.assertEqual(output.decode().split(), ["ok", "hidden", "readonly"])
        self.assertFalse((Path(self.temp.name) / "escape.txt").exists())

    def test_network_is_unavailable(self):
        code, output, _ = self.run_in_sandbox(
            f"{sys.executable} -c \"import socket; s=socket.socket(); s.settimeout(3); "
            "print(s.connect_ex(('1.1.1.1', 53)) != 0)\"")
        self.assertEqual((code, output.decode().strip()), (0, "True"))

    def test_confined_file_tool_round_trip(self):
        sandbox = ExecutionSandbox(self.workspace, "workspace")
        (self.workspace / "a.txt").write_text("hello\n", encoding="utf-8")
        result = asyncio.run(sandbox.tool("file_read_tool", {"file_path": str(self.workspace / "a.txt")}, {}))
        self.assertIn("hello", str(result))


if __name__ == "__main__":
    unittest.main()
