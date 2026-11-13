import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
from xuloom.models import get_adapter, unregister_adapter
from xuloom.plugins import PluginError, available_plugins, enabled_plugin_names, load_plugins, set_plugin_enabled

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "plugins" / "ollama-native"


class FakeOllama(BaseHTTPRequestHandler):
    script, requests = [], []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append({"path": self.path, "body": body})
        payload = type(self).script.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.home),
                                           "USERPROFILE": str(self.home)})
        self.env.start()
        FakeOllama.script, FakeOllama.requests = [], []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if get_adapter("ollama") is not None:
            unregister_adapter("ollama")
        sys.modules.pop("xuloom_plugin_ollama_native", None)
        self.env.stop()
        self.temp.cleanup()

    def install_example(self, enable=True):
        target = self.home / ".xuloom" / "plugins" / "ollama-native"
        shutil.copytree(EXAMPLE, target)
        if enable:
            set_plugin_enabled("ollama-native", True)
        return target

    def config(self, **changes):
        return EngineConfig(model="llama", model_provider="ollama", api_base_url=self.base, api_key="unused",
                            cwd=str(self.root / "work"), network_mode="direct", collaboration_mode="solo",
                            approval_policy=ApprovalPolicy("full"), load_enabled_plugins=True,
                            bundled_plugins=False).replace(**changes)

    async def test_directory_plugin_supplies_adapter_tool_hook_and_skill(self):
        self.install_example()
        FakeOllama.script = [
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "system_info", "arguments": {}}}]},
             "done_reason": "stop", "prompt_eval_count": 30, "eval_count": 4},
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "file_write_tool",
             "arguments": {"file_path": "notes.md", "content": "x\n"}}}]}, "done_reason": "stop"},
            {"message": {"role": "assistant", "content": "All done."}, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 3},
        ]
        async with QueryEngine(self.config()) as engine:
            self.assertEqual(engine.plugin_errors, {})
            self.assertIn("system_info", engine.available_tools)
            skills = {row["name"]: row for row in engine.tool_context["skill_registry"].discover()}
            self.assertEqual(skills["release-notes"]["scope"], "plugin")
            result = await engine.run("inspect and note", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "All done.")
        self.assertEqual((self.root / "work" / "notes.md").read_text(), "x\n")
        first, second, third = (item["body"] for item in FakeOllama.requests)
        self.assertEqual({item["path"] for item in FakeOllama.requests}, {"/api/chat"})
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertIn("system_info", {tool["function"]["name"] for tool in first["tools"]})
        self.assertIn('cpus', json.dumps(second["messages"]))
        self.assertIn("CHANGELOG.md", json.dumps(third["messages"]))
        self.assertNotIn("CHANGELOG.md", json.dumps(second["messages"]))

    async def test_installed_but_not_enabled_plugins_and_workspace_plugins_are_ignored(self):
        self.install_example(enable=False)
        workspace = self.root / "work" / ".xuloom" / "plugins" / "evil"
        workspace.mkdir(parents=True)
        (workspace / "plugin.py").write_text("raise SystemExit('workspace plugin executed')\n", encoding="utf-8")
        async with QueryEngine(self.config(model_provider="openai", api_base_url="http://127.0.0.1:9/v1")) as engine:
            self.assertEqual(engine.plugins, [])
            self.assertNotIn("system_info", engine.available_tools)
        rows = {row["name"]: row for row in available_plugins()}
        self.assertEqual(set(rows), {"media", "computer-use", "ollama-native"})
        self.assertFalse(rows["computer-use"]["enabled"])
        self.assertFalse(rows["ollama-native"]["enabled"])
        self.assertTrue(rows["media"]["enabled"] and rows["media"]["bundled"])

    async def test_broken_enabled_plugin_is_reported_without_blocking_the_engine(self):
        broken = self.home / ".xuloom" / "plugins" / "broken"
        broken.mkdir(parents=True)
        (broken / "plugin.py").write_text("def register(api):\n    raise RuntimeError('boom')\n", encoding="utf-8")
        set_plugin_enabled("broken", True)
        set_plugin_enabled("missing", True)
        async with QueryEngine(self.config(model_provider="openai", api_base_url="http://127.0.0.1:9/v1")) as engine:
            self.assertIn("boom", engine.plugin_errors["broken"])
            self.assertIn("not installed", engine.plugin_errors["missing"])

    async def test_explicit_plugins_fail_loudly_and_are_inherited_by_workers(self):
        class Bad:
            name = "bad"

            def register(self, api):
                api.add_hook("NoSuchEvent", lambda payload: None)
        with self.assertRaises(PluginError):
            QueryEngine(self.config(model_provider="openai", api_base_url="http://127.0.0.1:9/v1", plugins=[Bad()]))

        calls = []

        def register(api):
            calls.append(api.name)
            api.add_hook("TurnEnd", lambda payload: None)
        async with QueryEngine(self.config(model_provider="openai", api_base_url="http://127.0.0.1:9/v1",
                                           plugins=[register], load_enabled_plugins=False)) as engine:
            self.assertEqual(calls, ["register"])
            self.assertEqual(engine.tool_context["plugin_specs"], (register,))
            self.assertTrue(engine.hook_manager.has_callbacks("TurnEnd"))
            self.assertEqual(engine.config.to_public_dict()["plugins"], ["register"])

    async def test_failed_plugins_leave_no_partial_registration(self):
        from pydantic import BaseModel
        from xuloom import BaseAgenticTool
        from xuloom.models import ModelAdapter, adapter_names

        class Empty(BaseModel):
            pass

        def tool(name):
            return type("T", (BaseAgenticTool,), {"name": name, "description": "d", "input_schema": Empty,
                                                  "execute": lambda self: None})()

        class Partial(ModelAdapter):
            name = "partial-adapter"

        def raises_after_adapter(api):
            api.add_model_adapter(Partial())
            raise RuntimeError("late failure")

        def conflicting(api):
            api.add_model_adapter(Partial())
            api.add_tool(tool("plugin_ok_tool"))
            api.add_tool(tool("file_write_tool"))  # collides with a built-in
            api.add_hook("BeforeToolUse", lambda payload: {"decision": "reject"})

        base = self.config(model_provider="openai", api_base_url="http://127.0.0.1:9/v1", load_enabled_plugins=False)
        for plugin in (raises_after_adapter, conflicting):
            with self.assertRaises(Exception):
                QueryEngine(base.replace(plugins=[plugin]))
            self.assertNotIn("partial-adapter", adapter_names())

        for name, register in (("late", raises_after_adapter), ("conflict", conflicting)):
            folder = self.home / ".xuloom" / "plugins" / name
            folder.mkdir(parents=True)
            source = {"late": "def register(api):\n    from xuloom.models import ModelAdapter\n"
                              "    api.add_model_adapter(type('A', (ModelAdapter,), {'name': 'partial-adapter'})())\n"
                              "    raise RuntimeError('late failure')\n",
                      "conflict": "from pydantic import BaseModel\nfrom xuloom import BaseAgenticTool\nfrom xuloom.models import ModelAdapter\n"
                                  "class E(BaseModel):\n    pass\n"
                                  "def tool(n):\n    return type('T', (BaseAgenticTool,), {'name': n, 'description': 'd', 'input_schema': E, 'execute': lambda self: None})()\n"
                                  "def register(api):\n    api.add_model_adapter(type('A', (ModelAdapter,), {'name': 'partial-adapter'})())\n"
                                  "    api.add_tool(tool('plugin_ok_tool'))\n    api.add_tool(tool('file_write_tool'))\n"
                                  "    api.add_hook('BeforeToolUse', lambda payload: {'decision': 'reject'})\n"}[name]
            (folder / "plugin.py").write_text(source, encoding="utf-8")
            set_plugin_enabled(name, True)
        async with QueryEngine(base.replace(load_enabled_plugins=True)) as engine:
            self.assertIn("late failure", engine.plugin_errors["late"])
            self.assertIn("already registered", engine.plugin_errors["conflict"])
            self.assertNotIn("plugin_ok_tool", engine.available_tools)
            self.assertFalse(engine.hook_manager.has_callbacks("BeforeToolUse"))
            self.assertEqual(engine.tool_context["plugin_specs"], ())
        self.assertNotIn("partial-adapter", adapter_names())

    async def test_failed_plugin_that_provides_the_model_stops_the_engine(self):
        from xuloom.models import adapter_names
        from xuloom.plugins import PluginError
        folder = self.home / ".xuloom" / "plugins" / "guarded"
        folder.mkdir(parents=True)
        calls = self.root / "model-calls.txt"
        (folder / "plugin.py").write_text(
            "from pydantic import BaseModel\nfrom xuloom import BaseAgenticTool\n"
            "from xuloom.models import ModelAdapter, model_response\n"
            "class E(BaseModel):\n    pass\n"
            "class Guarded(ModelAdapter):\n    name = 'guarded'\n"
            "    async def stream_turn(self, request):\n"
            f"        open({str(calls)!r}, 'a').write('called')\n"
            "        return model_response(text='ran without its guard')\n"
            "def register(api):\n    api.add_model_adapter(Guarded())\n"
            "    api.add_tool(type('T', (BaseAgenticTool,), {'name': 'file_write_tool', 'description': 'd', 'input_schema': E, 'execute': lambda self: None})())\n"
            "    api.add_hook('BeforeToolUse', lambda payload: {'decision': 'reject'})\n", encoding="utf-8")
        set_plugin_enabled("guarded", True)
        with self.assertRaisesRegex(PluginError, "provides this engine's model adapter"):
            QueryEngine(self.config(model_provider="guarded", api_base_url="http://127.0.0.1:9"))
        self.assertNotIn("guarded", adapter_names())
        self.assertFalse(calls.exists())

    def test_enabled_list_validation(self):
        path = self.home / ".xuloom" / "plugins.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"enabled": ["../escape"]}), encoding="utf-8")
        with self.assertRaises(PluginError):
            enabled_plugin_names()
        with self.assertRaises(PluginError):
            set_plugin_enabled("Bad Name", True)
        with self.assertRaises(PluginError):
            load_plugins(["../escape"])

    def test_cli_lists_and_enables_plugins(self):
        self.install_example(enable=False)
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        run = lambda *args: subprocess.run([sys.executable, "-m", "xuloom", "plugins", *args, "--json"], cwd=ROOT,
                                           env=env, capture_output=True, text=True, timeout=60)
        enabled = run("enable", "ollama-native")
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        listed = json.loads(run("list").stdout.strip().splitlines()[-1])["result"]
        self.assertTrue({row["name"]: row for row in listed["plugins"]}["ollama-native"]["enabled"])
        self.assertNotEqual(run("enable", "not-installed").returncode, 0)


if __name__ == "__main__":
    unittest.main()
