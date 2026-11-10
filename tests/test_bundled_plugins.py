"""The media feature ships as a bundled plugin and uses only the public plugin API."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom import EngineConfig, QueryEngine
from xuloom.plugins import BUNDLED_PLUGINS, available_plugins, default_plugin_names, set_plugin_enabled
from xuloom.tools.builtin import default_tools

ROOT = Path(__file__).resolve().parents[1]


class BundledMediaPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.home),
                                           "USERPROFILE": str(self.home)})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def engine(self, **changes):
        return QueryEngine(EngineConfig(model="m", model_provider="openai", api_base_url="http://127.0.0.1:9/v1",
                                        api_key="k", cwd=str(self.root / "work"), network_mode="direct").replace(**changes))

    async def test_media_tool_comes_from_the_plugin_and_stays_latent(self):
        self.assertIn("media", BUNDLED_PLUGINS)
        self.assertNotIn("media_tool", default_tools())
        async with self.engine() as engine:
            self.assertEqual([(plugin.name, plugin.source) for plugin in engine.plugins], [("media", "bundled:xuloom.media.plugin")])
            self.assertIn("media_tool", engine.latent_tools)
            self.assertNotIn("media_tool", engine.available_tools)
            self.assertEqual(engine.activate_tools(["media_tool"]), ["media_tool"])
            self.assertIs(engine.available_tools["media_tool"].context, engine.tool_context)
            self.assertEqual(engine.tool_context["plugin_specs"], ("media",))

    async def test_media_can_be_turned_off_by_hosts_and_users(self):
        for changes in ({"bundled_plugins": False}, {"disabled_plugins": {"media"}}):
            async with self.engine(**changes) as engine:
                self.assertNotIn("media_tool", engine.latent_tools)
                self.assertEqual(engine.plugins, [])
        set_plugin_enabled("media", False)
        self.assertEqual(default_plugin_names(), [])
        async with self.engine(load_enabled_plugins=True) as engine:
            self.assertNotIn("media_tool", engine.latent_tools)
        # Programmatic hosts do not read the user's plugin settings.
        async with self.engine() as engine:
            self.assertIn("media_tool", engine.latent_tools)
        rows = {row["name"]: row for row in available_plugins()}
        self.assertFalse(rows["media"]["enabled"])
        set_plugin_enabled("media", True)
        self.assertEqual(default_plugin_names(), ["media"])

    async def test_host_disabled_tools_also_cover_plugin_tools(self):
        async with self.engine(disabled_tools={"media_tool"}) as engine:
            self.assertNotIn("media_tool", engine.latent_tools)
            self.assertNotIn("media_tool", engine.available_tools)
            self.assertEqual(engine.activate_tools(["media_tool"]), [])
            self.assertEqual(engine.plugin_errors, {})

    async def test_workers_inherit_the_parent_plugin_set_exactly(self):
        async with self.engine(plugins=["media"], bundled_plugins=False) as child:
            self.assertIn("media_tool", child.latent_tools)
        async with self.engine(plugins=[], bundled_plugins=False) as child:
            self.assertNotIn("media_tool", child.latent_tools)

    async def test_desktop_rpc_methods_are_served_by_the_plugin(self):
        from xuloom.desktop.bridge import DesktopBridge
        bridge = DesktopBridge(lambda event: None, self.root / "work")
        self.addAsyncCleanup(bridge.close)
        self.assertEqual(await bridge.dispatch("media.connections", {}), {"connections": []})
        self.assertEqual(await bridge.dispatch("media.jobs", {}), {"jobs": []})
        with self.assertRaisesRegex(ValueError, "请打开已有会话"):
            await bridge.dispatch("media.control", {"action": "status", "jobId": "x"})
        listed = {item["name"]: item for item in (await bridge.dispatch("plugins.rpc", {}))["methods"]}
        self.assertEqual(listed["media.control"], {"name": "media.control", "operation": True, "settings": False})
        self.assertEqual(listed["media.save"], {"name": "media.save", "operation": False, "settings": True})
        self.assertEqual(listed["media.connections"], {"name": "media.connections", "operation": False, "settings": False})
        set_plugin_enabled("media", False)
        disabled = DesktopBridge(lambda event: None, self.root / "work")
        self.addAsyncCleanup(disabled.close)
        with self.assertRaisesRegex(ValueError, "Unknown method: media.connections"):
            await disabled.dispatch("media.connections", {})
        self.assertEqual((await disabled.dispatch("plugins.rpc", {}))["methods"], [])

    def plugin(self, name, source):
        folder = self.home / ".xuloom" / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "plugin.py").write_text(source, encoding="utf-8")
        set_plugin_enabled(name, True)

    def run_cli(self, *args):
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        return subprocess.run([sys.executable, "-m", "xuloom", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_a_broken_plugin_command_does_not_break_other_commands(self):
        self.plugin("broken-cli", "def configure(parser):\n    raise RuntimeError('bad options')\n"
                                  "async def handle(args, emit):\n    return {}\n"
                                  "def register(api):\n    api.add_cli_command('broken-cli', configure, handle)\n")
        for args in (("exec", "--help"), ("plugins", "list"), ("media", "models", "--json")):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0, (args, result.stderr))
            self.assertIn("Plugin command 'broken-cli' is unavailable: RuntimeError: bad options", result.stderr)
        self.assertNotEqual(self.run_cli("broken-cli").returncode, 0)

    def test_minimal_plugin_command_gets_common_options_from_the_framework(self):
        self.plugin("hello", "def configure(parser):\n    parser.add_argument('who', nargs='?', default='world')\n"
                             "async def handle(args, emit):\n    return {'text': 'hello ' + args.who}\n"
                             "def register(api):\n    api.add_cli_command('hello', configure, handle)\n")
        plain = self.run_cli("hello", "xuloom")
        self.assertEqual((plain.returncode, plain.stdout.strip()), (0, "hello xuloom"), plain.stderr)
        as_json = self.run_cli("hello", "--json")
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        self.assertEqual(json.loads(as_json.stdout.strip().splitlines()[-1])["result"]["text"], "hello world")

    def test_cli_command_comes_from_the_plugin(self):
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        run = lambda *args: subprocess.run([sys.executable, "-m", "xuloom", *args], cwd=ROOT, env=env,
                                           capture_output=True, text=True, timeout=60)
        listed = run("media", "models", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout.strip().splitlines()[-1])["result"]["models"], [])
        self.assertEqual(run("plugins", "disable", "media").returncode, 0)
        disabled = run("media", "models", "--json")
        self.assertEqual(disabled.returncode, 2)
        self.assertIn("xuloom plugins enable media", disabled.stderr)


if __name__ == "__main__":
    unittest.main()
