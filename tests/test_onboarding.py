"""First-contact experience: install footprint, help text and the unconfigured-model message."""
import asyncio
import builtins
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def clean_env(folder):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XULOOM_", "OPENAI_", "ANTHROPIC_"))}
    env.update(XULOOM_CONFIG_DIR=folder + "/config", XULOOM_DATA_DIR=folder + "/data", HOME=folder + "/home",
               USERPROFILE=folder + "/home", PYTHONPATH=str(ROOT))
    return env


class PackagingTests(unittest.TestCase):
    def test_agpl_pdf_support_is_an_optional_extra(self):
        setup = (ROOT / "setup.py").read_text(encoding="utf-8")
        required = setup.split("install_requires=[", 1)[1].split("]", 1)[0]
        self.assertNotIn("pymupdf", required.lower())
        self.assertRegex(setup, r'"pdf":\s*\["pymupdf"\]')
        # The desktop runtime still bundles PDF reading.
        self.assertIn('[mcp,pdf]', (ROOT / "desktop/scripts/prepare-runtime.py").read_text(encoding="utf-8"))

    def test_pdf_read_without_the_extra_explains_how_to_install_it(self):
        from xuloom.tools.file_read_tool import FileReadTool
        real_import = builtins.__import__

        def no_pymupdf(name, *args, **kwargs):
            if name == "pymupdf":
                raise ImportError("No module named 'pymupdf'")
            return real_import(name, *args, **kwargs)
        with patch.object(builtins, "__import__", no_pymupdf), self.assertRaises(ValueError) as caught:
            FileReadTool()._pdf("unused.pdf", None, None, None, "auto")
        self.assertIn('pip install "xuloom[pdf]"', str(caught.exception))


class HelpTests(unittest.TestCase):
    def test_top_level_help_lists_the_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "xuloom", "--help"], env=clean_env(folder),
                                    cwd=folder, text=True, encoding="utf-8", capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = set(re.findall(r"^  ([a-z][a-z-]+)\s", result.stdout.split("commands (", 1)[1], re.MULTILINE))
        self.assertTrue({"exec", "serve", "eval", "plugins", "goal", "remote"} <= listed, listed)


class NotConfiguredTests(unittest.TestCase):
    def test_library_users_are_told_how_to_configure(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, clean_env(folder), clear=True):
            from xuloom import QueryEngine

            async def run():
                async with QueryEngine(cwd=folder) as engine:
                    return await engine.run("hi", sys_print_callback=lambda *_: None)
            result = asyncio.run(run())
        self.assertEqual(result.reason, "not_configured")
        for hint in ("Model not configured", "EngineConfig(", "OPENAI_API_KEY", "/model"):
            self.assertIn(hint, result.text)

    def test_cli_prints_one_english_explanation_and_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "xuloom", "exec", "hello"], env=clean_env(folder),
                                    cwd=folder, text=True, encoding="utf-8", capture_output=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OPENAI_API_KEY", result.stdout)
        self.assertIsNone(re.search(r"[一-鿿]", result.stdout), result.stdout)


if __name__ == "__main__":
    unittest.main()
