import asyncio
import tempfile
import unittest
import os
import json
from pathlib import Path

from xuloom.desktop.bridge import DesktopBridge
from xuloom.tools.plan_mode_tools import ExitPlanModeTool
from xuloom.features.plans import PlanManager
from xuloom.context.attachments import AttachmentCollector
from unittest.mock import patch, AsyncMock, Mock


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_dispatch_only_accepts_latest_stopped_task(self):
        from types import SimpleNamespace
        original = {"role": "user", "_uuid": "original", "content": "task"}
        trace = {"id": "same", "userMessageId": "original", "status": "cancelled", "steps": [], "files": [], "answer": "partial"}
        engine = SimpleNamespace(messages=[original], turn_traces=[trace], goal=None, get_mode=Mock(return_value="plan"),
            tool_context={"subagent_tasks": SimpleNamespace(tasks={}), "command_sessions": SimpleNamespace(records={})})
        self.bridge.engine = engine
        self.bridge.ensure_engine = AsyncMock(return_value=engine)
        self.bridge.turn = AsyncMock()
        self.assertEqual(await self.bridge.dispatch("prompt.resume", {}), {"started": True})
        await self.bridge.turn_task
        self.bridge.turn.assert_awaited_once_with("", [], "plan", resume_trace=trace)
        trace["status"] = "completed"
        with self.assertRaises(ValueError): await self.bridge.dispatch("prompt.resume", {})
        trace["status"] = "cancelled"
        engine.messages.append({"role": "user", "_uuid": "new-task", "content": "new task"})
        with self.assertRaises(ValueError): await self.bridge.dispatch("prompt.resume", {})
        engine.messages.pop()
        engine.goal = {"status": "paused"}
        with self.assertRaises(ValueError): await self.bridge.dispatch("prompt.resume", {})

    async def test_create_capabilities_validates_and_never_overwrites(self):
        base = dict(kind="skills", scope="project", name="evidence-check", description="Check evidence", prompt="Read original sources")
        snapshot = await self.bridge.dispatch("capabilities.create", base)
        self.assertTrue(any(s["name"] == "evidence-check" and s["valid"] for s in snapshot["skills"]))
        path = self.root / ".xuloom/skills/evidence-check/SKILL.md"
        original = path.read_bytes()
        with self.assertRaises(ValueError): await self.bridge.dispatch("capabilities.create", {**base, "prompt": "overwrite"})
        self.assertEqual(path.read_bytes(), original)
        for field in ({"name": "../escape"}, {"scope": "other"}, {"prompt": ""}):
            with self.assertRaises(ValueError): await self.bridge.dispatch("capabilities.create", {**base, **field})
        agent = dict(kind="agents", scope="project", name="evidence-reader", description="Read evidence", prompt="Verify the given claims", tools="Read, Skill", skills="evidence-check", model="inherit")
        snapshot = await self.bridge.dispatch("capabilities.create", agent)
        definition = self.bridge.agents.resolve("evidence-reader")
        self.assertEqual(definition["skills"], ["evidence-check"])
        self.assertEqual(definition["allowed_tools"], ["file_read_tool", "skill_tool"])
        with self.assertRaises(ValueError): await self.bridge.dispatch("capabilities.create", {**agent, "name": "bad-skill", "skills": "does-not-exist"})
        self.assertFalse((self.root / ".xuloom/agents/bad-skill.md").exists())

    async def test_goal_review_is_not_an_error_and_pause_is_cancelled(self):
        from types import SimpleNamespace
        for status, expected in (("awaiting_review", "completed"), ("paused", "cancelled"), ("blocked", "error")):
            with self.subTest(status=status):
                self.events.clear()
                goal = {"objective": "fixture goal", "status": status, "reason": "fixture reason",
                        "last_result": {"text": "saved result", "ok": True}}
                engine = SimpleNamespace(goal=goal, turn_traces=[], persist_session_state=Mock(return_value=True))
                self.bridge.engine = engine
                with patch("xuloom.features.goals.run", AsyncMock(return_value=goal)):
                    await self.bridge.goal_turn(engine)
                self.assertEqual(engine.turn_traces[-1]["status"], expected)
                self.assertEqual(any(event["type"] == "turn_error" for event in self.events), status == "blocked")
                self.assertEqual(engine.turn_traces[-1]["answer"], "saved result")
                engine.persist_session_state.assert_called_once()

    async def test_failed_scheduled_turn_cannot_reuse_a_previous_success_receipt(self):
        from types import SimpleNamespace
        self.bridge.engine = SimpleNamespace(last_run_result={"ok": True, "reason": "completed"}, session_id="fixture",
            session_manager=SimpleNamespace(owns_writer=Mock(return_value=False)))
        self.bridge.ensure_engine = AsyncMock(side_effect=ValueError("invalid scheduled skill"))
        result = await self.bridge.turn("Scheduled task", [], "normal")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "error")
    async def test_graceful_stop_is_cancelled_and_partial_text_is_retained_for_resume(self):
        from types import SimpleNamespace
        end = asyncio.Event()
        async def submit(text, **kwargs):
            yield {"type": "text_delta", "text": "PARTIAL_PROGRESS"}
            await end.wait()
            yield {"type": "final", "text": "internal abort response"}
        engine = SimpleNamespace(messages=[], turn_traces=[], set_mode=Mock(), submit_message=submit,
            session_id="fixture", session_manager=SimpleNamespace(owns_writer=Mock(return_value=False)),
            tool_context={"subagent_tasks": SimpleNamespace(close=AsyncMock()), "command_sessions": SimpleNamespace(close=AsyncMock())},
            request_abort=AsyncMock(side_effect=lambda: end.set()), persist_session_state=Mock(), _sync_message_context=Mock())
        self.bridge.engine = engine
        self.bridge.ensure_engine = AsyncMock(return_value=engine)
        self.bridge.turn_task = asyncio.create_task(self.bridge.turn("task", [], "normal"))
        while not any(event["type"] == "text_delta" for event in self.events): await asyncio.sleep(0)
        await self.bridge.dispatch("stop", {})
        await self.bridge.turn_task
        self.assertTrue(any(event["type"] == "turn_cancelled" for event in self.events))
        self.assertFalse(any(event["type"] == "final" for event in self.events))
        self.assertEqual(engine.turn_traces[-1]["status"], "cancelled")
        self.assertEqual(engine.turn_traces[-1]["answer"], "PARTIAL_PROGRESS")
        self.assertEqual(engine.messages[-1]["content"], "PARTIAL_PROGRESS")

    async def test_full_access_resolves_approvals_but_keeps_user_questions(self):
        permission = asyncio.create_task(self.bridge.ask("permission", {"request": {"tool_name": "file_write_tool"}}))
        plan = asyncio.create_task(self.bridge.ask("plan_approval", {"content": "plan"}))
        question = asyncio.create_task(self.bridge.ask("questions", {"questions": []}))
        await asyncio.sleep(0)
        result = await self.bridge.dispatch("permissions.set", {"mode": "full"})
        self.assertEqual(result["permissionMode"], "full")
        self.assertEqual(await permission, "allow")
        self.assertEqual(await plan, "allow")
        self.assertFalse(question.done())
        self.assertEqual(await self.bridge.ask("permission", {}), "allow")
        await self.bridge.dispatch("permissions.set", {"mode": "ask"})
        self.assertEqual((await self.bridge.dispatch("permissions.get", {}))["permissionMode"], "ask")
        question.cancel()
        await asyncio.gather(question, return_exceptions=True)
        self.assertEqual(self.bridge.pending_kinds, {})

    async def test_capability_examples_are_valid_and_never_overwrite_existing_files(self):
        with patch.dict(os.environ, {"XULOOM_SKILLS_PATHS": ""}):
            result = await self.bridge.dispatch("capabilities.example", {})
        self.assertTrue(any(skill["name"] == "project-review" and skill["valid"] for skill in result["skills"]))
        self.assertTrue(any(agent["name"] == "reviewer" and agent["valid"] for agent in result["agents"]))
        target = self.root / ".xuloom/skills/project-review/SKILL.md"
        original = target.read_bytes()
        with self.assertRaises(ValueError):
            await self.bridge.dispatch("capabilities.example", {})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(Path(result["settings"]["projectSkillsRoot"]).resolve(), (self.root / ".xuloom/skills").resolve())

    async def test_resumed_engine_uses_current_config_and_not_old_session_endpoint(self):
        cfg = {"model_provider": "openai", "openai_model": "current-model", "openai_base_url": "https://current.invalid/v1", "openai_api_key": "private-fixture", "network_mode": "direct"}
        engine = Mock(client=Mock(close=AsyncMock()), compactor=Mock(client=Mock(close=AsyncMock())))
        with patch("xuloom.desktop.bridge.load_config", return_value=cfg), patch.dict(os.environ):
            await self.bridge.sync_model(engine, force=True)
            engine.switch_model_runtime.assert_called_once_with(provider="openai", model="current-model", api_base_url="https://current.invalid/v1", api_key="private-fixture", network_mode="direct", persist=False)
            engine.client.close.assert_awaited_once()
            await self.bridge.sync_model(engine)
            self.assertEqual(engine.switch_model_runtime.call_count, 1)
            cfg["openai_base_url"] = "https://updated.invalid/v1"
            await self.bridge.sync_model(engine)
            self.assertEqual(engine.switch_model_runtime.call_count, 2)
            self.assertEqual(engine.switch_model_runtime.call_args.kwargs["api_base_url"], "https://updated.invalid/v1")

    async def test_model_can_save_without_project_engine_and_normalizes_full_endpoint(self):
        existing = {"openai_api_key": "fixture-existing-key", "other_setting": "keep"}
        with patch("xuloom.core.config.load_config", return_value=existing), patch("xuloom.core.config.save_config") as saved, patch("xuloom.core.config.CONFIG_FILE", self.root / "model-config.json"), patch.dict(os.environ, {}, clear=True):
            result = await self.bridge.dispatch("config.save", {"provider": "openai", "model": " model-id ", "baseUrl": "https://example.invalid/v1/chat/completions/", "apiKey": ""})
            self.assertEqual(result["baseUrl"], "https://example.invalid/v1")
            self.assertEqual(result["model"], "model-id")
            self.assertTrue(result["hasKey"])
            self.assertNotIn("fixture-existing-key", json.dumps(result))
            self.assertEqual(saved.call_args.args[0]["other_setting"], "keep")
            self.assertEqual(saved.call_args.args[0]["openai_api_key"], "fixture-existing-key")
            self.assertIsNone(self.bridge.engine)

    async def test_global_config_read_redacts_key(self):
        with patch("xuloom.desktop.bridge.load_config", return_value={"model_provider": "openai", "openai_model": "test-model", "openai_api_key": "fixture-private-key"}):
            result = await self.bridge.dispatch("config.get", {})
        self.assertEqual(result["model"], "test-model")
        self.assertTrue(result["hasKey"])
        self.assertNotIn("fixture-private-key", json.dumps(result))

    async def test_invalid_model_url_does_not_write_config(self):
        with patch("xuloom.core.config.CONFIG_FILE", self.root / "config.json"), patch("xuloom.core.config.save_config") as saved:
            with self.assertRaises(ValueError):
                await self.bridge.dispatch("config.save", {"provider": "openai", "model": "model", "baseUrl": "not-an-api-url"})
        saved.assert_not_called()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.bridge = DesktopBridge(self.events.append, self.root)

    async def test_initialization_does_not_require_model_or_create_engine(self):
        initial = await self.bridge.dispatch("initialize", {})
        self.assertEqual(initial["project"], str(self.root.resolve()))
        self.assertIsNone(self.bridge.engine)
        self.assertEqual(initial["history"], [])
        self.assertIn("explore", [agent["name"] for agent in initial["capabilities"]["agents"]])

    async def test_parallel_interactions_route_by_id(self):
        first = asyncio.create_task(self.bridge.ask("permission", {"request": {"tool_name": "one"}}))
        second = asyncio.create_task(self.bridge.ask("permission", {"request": {"tool_name": "two"}}))
        await asyncio.sleep(0)
        first_id, second_id = [event["requestId"] for event in self.events]
        await self.bridge.dispatch("respond", {"requestId": second_id, "answer": "deny"})
        self.assertEqual(await second, "deny")
        self.assertFalse(first.done())
        await self.bridge.dispatch("respond", {"requestId": first_id, "answer": "allow"})
        self.assertEqual(await first, "allow")
        with self.assertRaises(ValueError):
            await self.bridge.dispatch("respond", {"requestId": first_id, "answer": "allow"})

    async def test_stop_releases_waiting_approval(self):
        approval = asyncio.create_task(self.bridge.ask("permission", {}))
        await asyncio.sleep(0)
        await self.bridge.dispatch("stop", {})
        await asyncio.gather(approval, return_exceptions=True)
        self.assertEqual(self.bridge.pending, {})

    async def test_invalid_resume_id_cannot_escape_session_directory(self):
        with self.assertRaises(ValueError):
            await self.bridge.dispatch("initialize", {"sessionId": "../../config"})

    async def test_session_logs_do_not_feed_back_into_model_context(self):
        internal = self.root / ".xuloom" / "sessions" / "test.json"
        internal.parent.mkdir(parents=True)
        internal.write_text("internal history", encoding="utf-8")
        source = self.root / "app.py"
        source.write_text("print('hello')", encoding="utf-8")
        collector = AttachmentCollector()
        attachment = collector._build_file_change_delta(str(self.root), [str(internal), str(source)])
        self.assertNotIn(".xuloom", attachment["content"])
        self.assertIn("app.py", attachment["content"])

    async def test_imported_skills_are_available_without_enable_step(self):
        directory = self.root / "external" / "audit"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: audit\ndescription: Audit the project\n---\nCheck tests", encoding="utf-8")
        snapshot = await self.bridge.dispatch("capabilities.import", {"kind": "skills", "path": str(directory)})
        skill = snapshot["skills"][0]
        with self.assertRaises(ValueError):
            await self.bridge.dispatch("capabilities.toggle", {"id": skill["id"], "enabled": False})
        self.bridge.skills.update(disabled_skills=[skill["id"]])
        self.assertIn("Check tests", await self.bridge.dispatch("skills.read", {"name": "audit"}))
        restored = DesktopBridge(self.events.append, self.root)
        self.assertTrue(restored.skills.resolve("audit")["enabled"])

    async def test_plan_cannot_transition_until_approved(self):
        manager = PlanManager("desktop-plan-test")
        tool = ExitPlanModeTool()
        manager.set_mode("plan")
        waiting = asyncio.Event()
        async def approve(content):
            self.assertEqual(manager.get_mode(), "plan")
            await waiting.wait()
            return "deny"
        tool.context = {"plan_manager": manager, "plan_approval_callback": approve}
        with patch.object(manager, "get_plan", return_value="A concrete plan"):
            task = asyncio.create_task(tool.execute())
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            waiting.set()
            result = await task
            self.assertIn("not approved", result)
            self.assertEqual(manager.get_mode(), "plan")
            tool.context["plan_approval_callback"] = lambda content: "allow"
            await tool.execute()
            self.assertEqual(manager.get_mode(), "normal")


if __name__ == "__main__":
    unittest.main()
