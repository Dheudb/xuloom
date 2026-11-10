import os
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel

import xuloom
from xuloom import BaseAgenticTool, EngineConfig, QueryEngine


class EchoInput(BaseModel):
    value: str


class EchoTool(BaseAgenticTool):
    name = "echo_tool"
    description = "Echo the supplied value."
    input_schema = EchoInput
    is_read_only = True
    risk_level = "low"

    async def execute(self, value):
        return value


# Windows needs its OS paths even when provider/user configuration is isolated.
CLEAN_ENV = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}


class EngineConfigTests(unittest.TestCase):
    def test_resolved_applies_environment_defaults_once(self):
        env = {"XULOOM_MODEL_PROVIDER": "openai", "OPENAI_MODEL": "env-model", "OPENAI_BASE_URL": "http://env/v1",
               "OPENAI_API_KEY": "env-key", "XULOOM_COLLABORATION_MODE": "solo"}
        config = EngineConfig(cwd=".").resolved(env)
        self.assertEqual((config.model_provider, config.model, config.fallback_model), ("openai", "env-model", "env-model"))
        self.assertEqual((config.api_base_url, config.api_key), ("http://env/v1", "env-key"))
        self.assertEqual((config.collaboration_mode, config.execution_mode), ("solo", "host"))
        self.assertTrue(os.path.isabs(config.cwd))
        explicit = EngineConfig(model_provider="openai", model="m", api_base_url="", api_key="").resolved(env)
        self.assertEqual((explicit.model, explicit.api_base_url, explicit.api_key), ("m", "", ""))

    def test_replace_rejects_unknown_options_and_validates(self):
        with self.assertRaises(TypeError):
            EngineConfig().replace(not_an_option=1)
        with self.assertRaises(ValueError):
            EngineConfig(reasoning_effort="extreme")
        with self.assertRaises(ValueError):
            EngineConfig(agent_depth=-1)

    def test_public_dict_never_contains_credentials(self):
        config = EngineConfig(api_key="SECRET-KEY", extra_tools=[EchoTool()], disabled_tools=["repl_tool"])
        self.assertNotIn("SECRET-KEY", repr(config))
        data = config.to_public_dict()
        self.assertNotIn("SECRET-KEY", str(data))
        self.assertTrue(data["has_api_key"])
        self.assertEqual(data["extra_tools"], ["echo_tool"])
        self.assertEqual(data["disabled_tools"], ["repl_tool"])

    def test_top_level_package_exports_public_api(self):
        for name in ("QueryEngine", "EngineConfig", "BaseAgenticTool", "RunResult", "AbortRequestedError",
                     "PersistenceError", "ApprovalPolicy", "default_tools"):
            self.assertIn(name, xuloom.__all__)
            self.assertIsNotNone(getattr(xuloom, name))
        with self.assertRaises(AttributeError):
            xuloom.not_public


class EngineConstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {**CLEAN_ENV, "XULOOM_HOME": self.directory.name,
                                           "LOCALAPPDATA": self.directory.name, "XDG_DATA_HOME": self.directory.name,
                                           "HOME": self.directory.name, "USERPROFILE": self.directory.name}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def config(self, **changes):
        return EngineConfig(model="m", model_provider="openai", api_base_url="http://127.0.0.1:9/v1",
                            api_key="k", cwd=self.directory.name, network_mode="direct").replace(**changes)

    async def test_config_and_keyword_options_are_equivalent(self):
        async with QueryEngine(self.config()) as first, QueryEngine(**vars(self.config())) as second:
            self.assertEqual(first.config.to_public_dict()["model"], second.config.to_public_dict()["model"])
            self.assertEqual((first.primary_model, first.model_provider), ("m", "openai"))
        async with QueryEngine(self.config(), model="override") as engine:
            self.assertEqual(engine.primary_model, "override")
            self.assertEqual(engine.config.model, "override")
        with self.assertRaises(TypeError):
            QueryEngine({"model": "m"})
        with self.assertRaises(TypeError):
            QueryEngine(self.config(), bogus=True)

    async def test_extra_and_disabled_tools(self):
        async with QueryEngine(self.config(extra_tools=[EchoTool()], disabled_tools={"repl_tool", "web_fetch_tool"})) as engine:
            self.assertIn("echo_tool", engine.available_tools)
            self.assertIs(engine.available_tools["echo_tool"].context, engine.tool_context)
            self.assertNotIn("web_fetch_tool", engine.available_tools)
            self.assertNotIn("repl_tool", engine.latent_tools)
            self.assertNotIn("repl_tool", engine.builtin_tool_names)

    async def test_switch_model_runtime_keeps_config_in_sync(self):
        async with QueryEngine(self.config()) as engine:
            engine.switch_model_runtime(model="next", api_base_url="http://127.0.0.1:9/b/v1", api_key="k2", persist=False)
            self.assertEqual((engine.config.model, engine.config.api_base_url), ("next", "http://127.0.0.1:9/b/v1"))
            self.assertEqual(engine.config.api_key, "k2")


class ToolContextIsolationTests(unittest.TestCase):
    def test_each_tool_instance_owns_its_context(self):
        first, second = EchoTool(), EchoTool()
        first.context["marker"] = 1
        self.assertEqual(second.context, {})
        second.context = {"shared": True}
        self.assertEqual(first.context, {"marker": 1})


if __name__ == "__main__":
    unittest.main()
