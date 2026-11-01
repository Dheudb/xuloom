import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from xuloom.features.workspace_files import git, git_status
from xuloom.desktop.bridge import DesktopBridge


class GitStatusTests(unittest.IsolatedAsyncioTestCase):
    def command(self, cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE).decode().strip()

    async def test_unborn_dirty_clean_detached_and_branch_switch(self):
        with tempfile.TemporaryDirectory(prefix="xuloom git ") as directory:
            root = Path(directory)
            self.command(root, "init", "-b", "main")
            status = await git_status(root)
            self.assertEqual(status["branch"], "main"); self.assertTrue(status["unborn"]); self.assertFalse(status["dirty"])
            (root / "中文 space.txt").write_text("one\n")
            self.command(root, "add", ".")
            self.command(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
            self.assertFalse((await git_status(root))["dirty"])
            (root / "中文 space.txt").write_text("two\n"); self.command(root, "add", ".")
            (root / "中文 space.txt").write_text("three\n"); (root / "new.txt").write_text("new")
            (root / "sub").mkdir()
            status = await git_status(root / "sub")
            self.assertEqual((status["staged"], status["unstaged"], status["untracked"], status["changed"]), (1, 1, 1, 2))
            self.command(root, "checkout", "--detach")
            status = await git_status(root)
            self.assertTrue(status["detached"]); self.assertIsNone(status["branch"]); self.assertIsNotNone(status["commit"])
            self.command(root, "checkout", "-b", "feature/new")
            self.assertEqual((await git_status(root))["branch"], "feature/new")

    async def test_status_never_runs_repository_fsmonitor_or_inherits_other_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repo = root / "repo"; other = root / "other"
            repo.mkdir(); other.mkdir(); self.command(repo, "init", "-b", "own"); self.command(other, "init", "-b", "wrong")
            hook = repo / ".git/monitor.sh"; marker = root / "executed"
            hook.write_text('#!/bin/sh\necho called > "' + marker.as_posix() + '"\n')
            hook.chmod(0o755); self.command(repo, "config", "core.fsmonitor", hook.as_posix())
            with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
                status = await git_status(repo)
            self.assertEqual(status["branch"], "own"); self.assertFalse(marker.exists())
            self.assertEqual(self.command(repo, "config", "core.fsmonitor"), hook.as_posix())

    async def test_absent_git_non_repository_and_missing_workspace_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual((await git_status(Path(directory) / "missing"))["code"], "workspace_missing")
            self.assertEqual((await git_status(directory))["code"], "not_repository")
            with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
                self.assertEqual((await git_status(directory))["code"], "git_missing")

    async def test_cancelled_git_is_reaped(self):
        process = NS(returncode=None, kill=Mock(), communicate=AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")]))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(asyncio.CancelledError): await git(".", "status")
        process.kill.assert_called_once(); self.assertEqual(process.communicate.await_count, 2)

    async def test_repository_path_whitespace_is_not_trimmed(self):
        command = AsyncMock(side_effect=[(0, "/workspace with trailing space \n", ""),
            (0, "# branch.oid (initial)\0# branch.head main\0", "")])
        with patch("xuloom.features.workspace_files.Path.is_dir", return_value=True), patch("xuloom.features.workspace_files.git", command):
            result = await git_status("/requested")
        self.assertEqual(result["root"], "/workspace with trailing space ")
        self.assertEqual(command.await_args_list[1].args[0], result["root"])

    async def test_actual_merge_conflict_is_not_reported_as_committable_staged_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); file = root / "conflict.txt"
            self.command(root, "init", "-b", "main")
            def commit(text):
                file.write_text(text); self.command(root, "add", ".")
                self.command(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", text.strip())
            commit("base\n"); self.command(root, "checkout", "-b", "side"); commit("side\n")
            self.command(root, "checkout", "main"); commit("main\n")
            with self.assertRaises(subprocess.CalledProcessError) as conflict:
                self.command(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "side")
            self.assertIn(b"CONFLICT", conflict.exception.output)
            status = await git_status(root)
            self.assertEqual((status["conflicted"], status["staged"], status["changed"]), (1, 0, 1))
            self.assertTrue(status["dirty"])

    async def test_bridge_read_does_not_create_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            self.command(directory, "init", "-b", "bridge")
            bridge = DesktopBridge(lambda _: None, cwd=directory)
            with patch.object(bridge, "ensure_engine", AsyncMock(side_effect=AssertionError("No model/session engine"))):
                result = await bridge.dispatch("git.status", {})
            self.assertEqual(result["branch"], "bridge"); self.assertIsNone(bridge.engine)
            await bridge.close()

    async def test_cli_real_process_json_needs_no_model_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repo = root / "repo"; repo.mkdir(); self.command(repo, "init", "-b", "cli")
            env = {**os.environ, "XULOOM_CONFIG_DIR": str(root / "config"), "XULOOM_DATA_DIR": str(root / "data"), "HOME": str(root / "home"), "USERPROFILE": str(root / "home")}
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "xuloom.main", "git-status", "--cwd", str(repo), "--json"], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            receipt = json.loads(result.stdout.decode())
            self.assertEqual(receipt["result"]["git"]["branch"], "cli")
            self.assertFalse((root / "data/history").exists())
