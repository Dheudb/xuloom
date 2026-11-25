"""Portable Windows command registration, without touching the real user PATH."""
import copy
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CliInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "Application"
        (self.package / "resources/python").mkdir(parents=True)
        (self.package / "Xuloom.exe").write_bytes(b"app")
        (self.package / "resources/python/python.exe").write_bytes(b"runtime")
        self.bin = self.root / "Command bin"
        self.api = runpy.run_path(str(ROOT / "desktop/cli/cli_install.py"))
        self.environment = {"value": r"%SystemRoot%\Tools;C:\Existing", "kind": 2, "exists": True}
        self.writes = []
        def write(value):
            self.writes.append(copy.deepcopy(value))
            self.environment = copy.deepcopy(value)
        fake = patch.dict(self.api["install"].__globals__, {
            "read_user_path": lambda: copy.deepcopy(self.environment),
            "write_user_path": write, "notify_environment": lambda: None})
        fake.start()
        self.addCleanup(fake.stop)

    def install(self):
        return self.api["install"](self.package, self.bin)

    def test_install_reinstall_and_uninstall_preserve_unrelated_paths_and_files(self):
        original = self.environment["value"]
        first = self.install()
        self.assertEqual(Path(first["python"]), (self.package / "resources/python/python.exe").resolve())
        self.install()
        self.assertEqual(self.environment["value"], original + ";" + str(self.bin))
        self.assertEqual(len(self.writes), 1)
        self.environment["value"] += ";C:\\Later"
        (self.bin / "unrelated.txt").write_text("keep")
        self.api["uninstall"](self.bin)
        self.assertEqual(self.environment["value"], original + ";C:\\Later")
        self.assertEqual(self.environment["kind"], 2)
        self.assertFalse((self.bin / "xuloom.cmd").exists())
        self.assertTrue((self.bin / "unrelated.txt").exists())
        self.assertFalse(self.api["uninstall"](self.bin)["installed"])

    def test_existing_path_entry_remains_after_uninstall(self):
        self.environment["value"] = 'C:\\Before;"' + str(self.bin) + '";C:\\After'
        original = self.environment["value"]
        self.install()
        self.api["uninstall"](self.bin)
        self.assertEqual(self.environment["value"], original)
        self.assertEqual(self.writes, [])

    def test_previously_absent_path_is_removed_again(self):
        self.environment.update(value="", exists=False)
        self.install()
        self.api["uninstall"](self.bin)
        self.assertEqual(self.environment["value"], "")
        self.assertFalse(self.environment["exists"])

    def test_trailing_separators_and_invalid_bin_paths(self):
        original = self.environment["value"] + ";;"
        self.environment["value"] = original
        self.install()
        self.api["uninstall"](self.bin)
        self.assertEqual(self.environment["value"], original)
        with self.assertRaises(ValueError):
            self.api["install"](self.package, self.root / "bad;directory")

    def test_unowned_or_modified_commands_are_never_overwritten_or_removed(self):
        self.bin.mkdir()
        command = self.bin / "xuloom.cmd"
        command.write_bytes(b"somebody else's command")
        with self.assertRaises(ValueError):
            self.install()
        command.unlink()
        self.install()
        command.write_bytes(b"user edits")
        with self.assertRaises(ValueError):
            self.install()
        with self.assertRaises(ValueError):
            self.api["uninstall"](self.bin)
        self.assertEqual(command.read_bytes(), b"user edits")

    def test_registry_failure_rolls_back_new_launcher_files(self):
        with patch.dict(self.api["install"].__globals__, {"write_user_path": lambda _: (_ for _ in ()).throw(OSError("denied"))}):
            with self.assertRaises(OSError):
                self.install()
        self.assertFalse((self.bin / "xuloom.cmd").exists())
        self.assertFalse((self.bin / "cli-install.json").exists())

    def test_incomplete_package_is_rejected_before_registration(self):
        (self.package / "resources/python/python.exe").unlink()
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.bin.exists())

    def test_launcher_quotes_paths_and_never_changes_the_working_directory(self):
        text = self.api["launcher"](r"C:\中文 & %profile% !\python.exe").decode("utf-8")
        self.assertIn("%%profile%%", text)
        self.assertIn("DisableDelayedExpansion", text)
        self.assertIn("chcp 65001 >nul", text)
        self.assertIn("-I -X utf8 -m xuloom %*", text)
        self.assertNotIn("cd /d", text.lower())
        self.assertNotIn("call ", text.lower())
        self.assertIn("exit /b %_xuloom_exit%", text)

    @unittest.skipUnless(os.name == "nt", "Windows command forwarding")
    def test_real_cli_keeps_cwd_forwards_quoted_arguments_and_exit_codes(self):
        package = ROOT / "desktop/release/Xuloom-win32-x64"
        if not (package / "resources/python/python.exe").exists():
            self.skipTest("Built Windows runtime not available")
        self.api["install"](package, self.bin)
        command = self.bin / "xuloom.cmd"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_CONFIG_", "XULOOM_", "OPENAI_", "ANTHROPIC_"))}
        env.update(XULOOM_CONFIG_DIR=str(self.root / "config"), XULOOM_DATA_DIR=str(self.root / "data"),
                   HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"))
        work = self.root / "中文 project & one"
        other = self.root / "project two 空格"
        work.mkdir(); other.mkdir()
        for folder, branch in ((work, "one"), (other, "two")):
            subprocess.run(["git", "init", "-b", branch], cwd=folder, env=env, check=True, capture_output=True)
        def cli(arguments):
            return subprocess.run(f'"{os.environ["COMSPEC"]}" /d /s /c ""{command}" {arguments}"',
                                  cwd=work, env=env, capture_output=True, timeout=30)
        result = cli("git-status --json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["git"]["branch"], "one")
        result = cli(f'git-status --cwd "{other}" --json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["git"]["branch"], "two")
        result = cli("exec --no-such-option")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "data/history").exists())

    @unittest.skipUnless(os.name == "nt", "Windows Unicode batch paths")
    def test_real_runtime_under_unicode_percent_and_exclamation_path(self):
        original = ROOT / "desktop/release/Xuloom-win32-x64/resources/python"
        if not (original / "python.exe").is_file():
            self.skipTest("Built Windows runtime not available")
        package = self.root / "程序 & %XULOOM_UNEXPANDED% !"
        runtime = package / "resources/python"
        runtime.mkdir(parents=True)
        (package / "Xuloom.exe").write_bytes(b"app")
        # Only copy the interpreter and DLLs. Public read-only libraries stay in
        # the built runtime; no junctions and no dependency installation.
        for path in original.iterdir():
            if path.is_file() and (path.name == "python.exe" or path.suffix.lower() == ".dll"):
                shutil.copyfile(path, runtime / path.name)
        dll = next(path for path in original.glob("python3*.dll") if path.stem != "python3")
        paths = [str(path) for path in original.glob("python*.zip")]
        (runtime / (dll.stem + "._pth")).write_text("\n".join(paths + [str(original), str(original / "Lib"),
            str(original / "DLLs"), str(original / "Lib/site-packages"), "import site"]), encoding="utf-8")
        self.api["install"](package, self.bin)
        command = self.bin / "xuloom.cmd"
        env = {**os.environ, "XULOOM_UNEXPANDED": "wrong"}
        result = subprocess.run(f'"{os.environ["COMSPEC"]}" /d /s /c ""{command}" --help"',
                                env=env, cwd=self.root, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"usage: xuloom", result.stdout)


if __name__ == "__main__":
    unittest.main()
