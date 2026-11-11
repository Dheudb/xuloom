"""`xuloom plugins new` produces a plugin that loads, passes its own offline test and can be enabled."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = self.temp.name
        self.home = Path(folder) / "home"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("XULOOM_", "OPENAI_", "ANTHROPIC_"))}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), XULOOM_DATA_DIR=str(Path(folder) / "data"),
                        XULOOM_CONFIG_DIR=str(Path(folder) / "config"), PYTHONPATH=str(ROOT))

    def xuloom(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-m", "xuloom", *args], env=self.env, cwd=self.temp.name,
                              text=True, encoding="utf-8", capture_output=True, timeout=120)

    def test_new_plugin_runs_its_test_and_can_be_enabled(self):
        created = self.xuloom("plugins", "new", "demo-tools")
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        folder = self.home / ".xuloom" / "plugins" / "demo-tools"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["README.md", "plugin.py", "test_plugin.py"])
        self.assertIn("xuloom plugins enable demo-tools", created.stdout)

        check = subprocess.run([sys.executable, "-X", "utf8", str(folder / "test_plugin.py")], env=self.env,
                               cwd=self.temp.name, text=True, encoding="utf-8", capture_output=True, timeout=120)
        self.assertEqual(check.returncode, 0, check.stderr + check.stdout)
        self.assertIn("demo-tools: plugin OK", check.stdout)

        self.assertEqual(self.xuloom("plugins", "enable", "demo-tools").returncode, 0)
        listed = self.xuloom("plugins", "list")
        self.assertRegex(listed.stdout, r"\* demo-tools ")

    def test_existing_or_invalid_names_are_refused(self):
        self.assertEqual(self.xuloom("plugins", "new", "demo").returncode, 0)
        again = self.xuloom("plugins", "new", "demo")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)
        for name in ("Bad Name", "media"):
            with self.subTest(name=name):
                self.assertNotEqual(self.xuloom("plugins", "new", name).returncode, 0)


if __name__ == "__main__":
    unittest.main()
