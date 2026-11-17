"""Side chats are fresh local sessions; opening them never invokes a model."""
import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.core.config import update_config, load_config
from xuloom.core.model_catalog import save_connection
from xuloom.core.session import SessionManager
from xuloom.desktop.bridge import DesktopBridge


class SideChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = patch("xuloom.core.config.CONFIG_FILE", self.root / "config.json")
        config.start(); self.addCleanup(config.stop)
        data = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data")})
        data.start(); self.addCleanup(data.stop)
        cfg = save_connection({"model_connections": []}, {"connectionId": "fixture", "provider": "openai",
            "model": "one", "models": ["one", "two"], "baseUrl": "http://127.0.0.1:1/v1", "apiKey": "owned-fixture",
            "modelReasoning": {"two": {"adapter": "openai", "efforts": ["low", "high"], "effort": "high"}}})
        update_config(lambda _: cfg)
        self.parent = DesktopBridge(lambda _: None, self.root)
        self.addAsyncCleanup(self.parent.close)
        self.sid = (await self.parent.dispatch("sessions.create", {}))["sessionId"]
        await self.parent.dispatch("initialize", {"sessionId": self.sid})
        await self.parent.dispatch("config.select", {"connectionId": "fixture", "model": "two"})

    async def child(self, **params):
        created = await self.parent.dispatch("sessions.side", {"draft": "> selected original", **params})
        child = DesktopBridge(lambda _: None, self.root)
        self.addAsyncCleanup(child.close)
        initial = await child.dispatch("initialize", {"sessionId": created["sessionId"], "permissionMode": "ask"})
        return child, initial

    async def test_running_parent_survives_side_creation_without_calls_or_replay(self):
        self.parent.turn_task = asyncio.create_task(asyncio.sleep(60))
        before = SessionManager(cwd=self.root).load_session_state(self.sid)
        child, initial = await self.child(scope="standalone")
        self.assertTrue(self.parent.busy())
        self.assertFalse(self.parent.turn_task.cancelled())
        self.assertIsNone(self.parent.engine)
        self.assertIsNone(child.engine)
        self.assertEqual(initial["history"], [])
        self.assertEqual(initial["sideChat"], {"parentSessionId": self.sid})
        self.assertEqual((initial["config"]["model"], initial["config"]["modelReasoning"]["two"]["effort"]), ("two", "high"))
        self.assertEqual((initial["permissionMode"], initial["collaborationMode"]), ("ask", "solo"))
        self.assertEqual(SessionManager(cwd=self.root).load_session_state(self.sid), before)

    async def test_side_model_changes_do_not_change_parent_or_new_chat_preferences(self):
        before = copy.deepcopy(load_config())
        child, _ = await self.child()
        await child.dispatch("config.select", {"connectionId": "fixture", "model": "one"})
        self.assertEqual(load_config(), before)
        self.assertEqual(self.parent.config()["model"], "two")
        self.assertEqual(child.config()["model"], "one")

    async def test_relation_survives_engine_save_and_reopen(self):
        child, initial = await self.child()
        engine = await child.ensure_engine(write=True)
        self.assertTrue(engine.persist_session_state())
        sid = initial["persistedId"]
        await child.close()
        restored = DesktopBridge(lambda _: None, self.root)
        self.addAsyncCleanup(restored.close)
        initial = await restored.dispatch("initialize", {"sessionId": sid})
        self.assertEqual(initial["sideChat"]["parentSessionId"], self.sid)

    async def test_quoted_skill_name_is_data_not_an_implicit_skill_invocation(self):
        child, _ = await self.child()
        engine = await child.ensure_engine(write=True)
        text = "> Source\n> $not-an-installed-skill\n\nExplain this phrase."
        with patch.object(engine.tool_context["skill_registry"], "resolve", side_effect=AssertionError("Quoted data activated a skill")):
            result = await engine._apply_selected_skills(text, None, None, [], None, None)
        self.assertEqual(result[0], text)

    async def test_nested_invalid_or_unowned_creation_is_refused(self):
        child, _ = await self.child()
        for bridge, params in [(child, {}), (self.parent, {"draft": "x" * 20001}), (self.parent, {"scope": "other"})]:
            with self.assertRaises(ValueError):
                await bridge.dispatch("sessions.side", params)
        fresh = DesktopBridge(lambda _: None, self.root)
        self.addAsyncCleanup(fresh.close)
        with self.assertRaises(ValueError):
            await fresh.dispatch("sessions.side", {})
