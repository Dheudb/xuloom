from xuloom.core.run_result import RunResult
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from xuloom.skills.registry import SkillRegistry
from xuloom.agents.registry import AgentRegistry
from xuloom.agents.tasks import SubagentTasks
from xuloom.agents.collaboration import policy as collaboration_policy


class RegistryTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"XULOOM_SKILLS_PATHS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.home = self.root / "home"
        self.project.mkdir()
        self.home.mkdir()
        self.registry = SkillRegistry(self.project, self.home)

    def skill(self, root, name="review", body="Read references/checklist.md"):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: " + name + "\ndescription: Review code carefully\n---\n" + body, encoding="utf-8")
        return directory

    def test_project_precedence_ignores_legacy_disabled_flags(self):
        project = self.skill(self.project / ".xuloom" / "skills")
        self.skill(self.home / ".xuloom" / "skills")
        self.assertEqual(self.registry.resolve("review")["root"], str(project.resolve()))
        self.assertEqual(sum(s["shadowed"] for s in self.registry.discover()), 1)
        self.registry.update(disabled_skills=[self.registry.resolve("review")["id"]])
        self.assertEqual(self.registry.resolve("review")["scope"], "project")

    def test_invalid_frontmatter_is_visible_and_not_active(self):
        root = self.skill(self.project / ".xuloom" / "skills")
        (root / "SKILL.md").write_text("---\nname: wrong-name\ndescription: test\n---\nInstructions", encoding="utf-8")
        self.assertFalse(self.registry.discover()[0]["valid"])
        self.assertFalse(any(s["scope"] == "project" for s in self.registry.active()))
        with self.assertRaises(ValueError):
            self.registry.read("review")

    def test_imported_resources_resolve_from_skill_root(self):
        root = self.skill(self.root / "outside")
        (root / "references").mkdir()
        (root / "references" / "checklist.md").write_text("Unique checklist", encoding="utf-8")
        self.registry.update(skill_roots=[str(root)])
        self.assertIn(str(root.resolve()), self.registry.read("review"))
        self.assertEqual(self.registry.read_resource("review", "references/checklist.md"), "Unique checklist")
        with self.assertRaises(ValueError):
            self.registry.read_resource("review", "../../secret.txt")

    def test_custom_agent_and_required_skills(self):
        skill = self.skill(self.project / ".xuloom" / "skills")
        agents = self.project / ".xuloom" / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: Review implementation\ntools: Read, Grep, Glob, Skill\nmodel: local-review-model\nskills: [review]\n---\nOnly review, never edit.", encoding="utf-8")
        registry = AgentRegistry(self.project, self.home)
        definition = registry.resolve("reviewer")
        self.assertIn("file_read_tool", definition["allowed_tools"])
        self.assertEqual(definition["model"], "local-review-model")
        (skill / "SKILL.md").unlink()
        self.assertFalse(registry.discover()[0]["valid"])
        with self.assertRaises(ValueError):
            registry.resolve("reviewer")

    def test_refresh_sees_new_skills(self):
        initial = len(self.registry.active())
        self.skill(self.project / ".agents" / "skills")
        self.assertEqual(len(self.registry.active()), initial + 1)

    def test_bundled_skills_load_without_user_configuration(self):
        builtins = [s for s in self.registry.active() if s["scope"] == "builtin"]
        self.assertTrue({"code-review", "debug-issue", "web-research"}.issubset({s["name"] for s in builtins}))
        for skill in builtins:
            self.assertTrue(Path(skill["path"]).is_file())
            self.assertIn("Project directory: " + str(self.project.resolve()), self.registry.read(skill["name"]))
        for root, scope in self.registry.roots("agents"):
            if scope == "builtin":
                self.assertTrue((root.parent / "SKILL.md").is_file())

    def test_custom_skill_overrides_builtin_and_removal_restores_fallback(self):
        directory = self.skill(self.project / ".xuloom" / "skills", name="code-review", body="CUSTOM_REVIEW_INSTRUCTIONS")
        custom = self.registry.resolve("code-review")
        self.assertEqual(custom["scope"], "project")
        self.assertIn("CUSTOM_REVIEW_INSTRUCTIONS", self.registry.read("code-review"))
        self.registry.update(disabled_skills=[custom["id"]])
        self.assertEqual(self.registry.resolve("code-review")["scope"], "project")
        (directory / "SKILL.md").unlink()
        builtin = self.registry.resolve("code-review")
        self.assertEqual(builtin["scope"], "builtin")
        self.registry.update(disabled_skills=[custom["id"], builtin["id"]])
        self.assertIn("Skill: code-review", self.registry.read("code-review"))


class TaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_completion_and_delivery_once(self):
        manager = SubagentTasks()
        gate = asyncio.Event()
        entered = []
        async def work(identity):
            entered.append(identity)
            await gate.wait()
            return {"agent_id": identity, "status": "completed", "result": identity}
        manager.launch("one", work("one"))
        manager.launch("two", work("two"))
        await asyncio.sleep(0)
        self.assertEqual(entered, ["one", "two"])
        gate.set()
        await asyncio.gather(*manager.tasks.values())
        self.assertEqual(len(manager.completed()), 2)
        self.assertEqual(manager.completed(), [])

    async def test_cancel_and_limit(self):
        manager = SubagentTasks(limit=1, queue_limit=0)
        gate = asyncio.Event()
        manager.launch("one", gate.wait())
        with self.assertRaises(ValueError):
            manager.launch("two", gate.wait())
        await asyncio.sleep(0)
        result = await manager.cancel("one")
        self.assertEqual(result["status"], "cancelled")
        await manager.close()

    async def test_wait_collects_real_results_and_does_not_deliver_twice(self):
        from xuloom.agents.agent_tool import AgentTool
        manager = SubagentTasks()
        gate = asyncio.Event()
        async def child():
            await gate.wait()
            return {"agent_id": "one", "status": "completed", "result": "child answer"}
        manager.launch("one", child())
        tool = AgentTool(); tool.context = {"subagent_tasks": manager}
        waiting = asyncio.create_task(tool.execute(action="wait"))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        gate.set()
        self.assertIn("child answer", await waiting)
        self.assertEqual(manager.completed(), [])
        await manager.close()


class EngineSubagentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Minimal legacy tool contexts still discover native roles. Never let
        # that discovery consult a developer's personal mounts or configuration.
        scratch = tempfile.TemporaryDirectory(prefix="xuloom-transient-agents-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        environment = patch.dict(os.environ, {"HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"), "XULOOM_CONFIG_DIR": str(root / "config"),
            "XULOOM_DATA_DIR": str(root / "data"), "XULOOM_SKILLS_PATHS": ""})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_one_call_starts_ten_before_any_reply(self):
        from xuloom.agents.agent_tool import AgentTool
        tool = AgentTool()
        records, entered = {}, []
        def register(record):
            records.setdefault(record["agent_id"], {}).update(record)
        tool.context = {"collaboration": collaboration_policy("auto", {"agent_concurrency": 10}), "register_subagent_record": register}
        gate = asyncio.Event()
        async def child(**kwargs):
            self.assertEqual(len(records), 10, "All cards must be registered before the first request")
            entered.append(kwargs["child_agent_id"])
            if len(entered) == 10:
                gate.set()
            await gate.wait()
            return {"agent_id": kwargs["child_agent_id"], "status": "completed", "result": kwargs["description"]}
        with patch.object(tool, "_run_single_subagent", side_effect=child):
            report = await asyncio.wait_for(tool.execute(prompt="你好", description="问候", count=10), 2)
        self.assertEqual(len(set(entered)), 10)
        self.assertIn("Completed: 10", report)
        for index in range(1, 11):
            self.assertIn(f"问候 {index}/10", report)

    async def test_background_batch_wait_and_capacity_are_atomic(self):
        from xuloom.agents.agent_tool import AgentTool
        tool = AgentTool()
        records, entered = {}, []
        def register(record):
            records.setdefault(record["agent_id"], {}).update(record)
        tool.context = {"collaboration": collaboration_policy("auto", {"agent_concurrency": 10}), "register_subagent_record": register}
        gate = asyncio.Event()
        async def child(**kwargs):
            entered.append(kwargs["child_agent_id"])
            await gate.wait()
            return {"agent_id": kwargs["child_agent_id"], "status": "completed", "result": "你好"}
        with patch.object(tool, "_run_single_subagent", side_effect=child):
            response = await tool.execute(prompt="你好", count=10, run_in_background=True)
            self.assertIn("count=10", response)
            self.assertEqual(len(records), 10)
            await asyncio.sleep(0)
            self.assertEqual(len(entered), 10)
            await tool.execute(prompt="extra", count=2, run_in_background=True)
            self.assertEqual(len(records), 12)
            self.assertEqual(len(tool.context["subagent_tasks"].pending), 2)
            gate.set()
            report = await tool.execute(action="wait")
            self.assertIn("Completed: 12", report)
            self.assertEqual(tool.context["subagent_tasks"].completed(), [])
        await tool.context["subagent_tasks"].close()

    async def test_parallel_batches_share_ten_slots_and_early_cancel_settles_cards(self):
        from xuloom.agents.agent_tool import AgentTool
        tool = AgentTool()
        rows, entered = {}, []
        def register(record):
            rows.setdefault(record["agent_id"], {}).update(record)
        tool.context = {"collaboration": collaboration_policy("auto", {"agent_concurrency": 10}), "register_subagent_record": register}
        gate = asyncio.Event()
        async def child(**kwargs):
            entered.append(kwargs["child_agent_id"])
            await gate.wait()
            return {"agent_id": kwargs["child_agent_id"], "status": "completed", "result": "你好"}
        with patch.object(tool, "_run_single_subagent", side_effect=child):
            batches = [asyncio.create_task(tool.execute(prompt="你好", count=10)) for _ in range(2)]
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(entered), 10)
            gate.set()
            outcomes = await asyncio.gather(*batches, return_exceptions=True)
            self.assertEqual(sum(isinstance(outcome, ValueError) for outcome in outcomes), 0,
                             "Excess foreground work must queue until slots free")
            self.assertEqual(len(set(entered)), 20)
            await tool.execute(prompt="你好", count=10)
            self.assertEqual(len(set(entered)), 30)
            await tool.execute(prompt="你好", count=10, run_in_background=True)
            manager = tool.context["subagent_tasks"]
            # Cancel without yielding after allocation: child code never starts.
            await manager.cancel(next(iter(manager.tasks)))
            await manager.close()
            self.assertTrue(all(rows[identity]["status"] == "cancelled" for identity, result in manager.results.items() if result["status"] == "cancelled"))

    async def test_batch_initialization_failure_and_invalid_count(self):
        from xuloom.agents.agent_tool import AgentTool, AgentToolInput
        tool = AgentTool()
        records = {}
        def register(record):
            records.setdefault(record["agent_id"], {}).update(record)
        tool.context = {"collaboration": collaboration_policy("auto", {"agent_concurrency": 10}), "register_subagent_record": register}
        for count in [0, 11, True, 1.5, "10"]:
            with self.assertRaises(ValueError):
                AgentToolInput(count=count)
            with self.assertRaises(ValueError):
                await tool.execute(prompt="你好", count=count)
        self.assertEqual(records, {})
        with patch.object(tool, "_run_single_subagent", side_effect=RuntimeError("client initialization failed")):
            report = await tool.execute(prompt="你好", count=10)
        self.assertIn("Crashed: 10", report)
        self.assertTrue(all(record["status"] == "crashed" and record["finished_at"] for record in records.values()))

    async def test_model_override_tools_team_registration_and_background(self):
        from xuloom.core.engine import QueryEngine
        from xuloom.memory.memory import MemoryCompactor
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OPENAI_API_KEY": "test", "XULOOM_MODEL_PROVIDER": "openai", "XULOOM_DATA_DIR": str(Path(temp) / "data")}), patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None):
            original_cwd = os.getcwd()
            os.chdir(temp)
            try:
                engine = QueryEngine(model="parent-model", model_provider="openai", cwd=temp)
                previous_client, previous_compactor = engine.client, engine.compactor
                engine.switch_model_runtime(model="parent-model")
                self.assertIs(engine.session_memory_manager.compactor, engine.compactor)
                self.assertIs(engine.memory_extractor.compactor, engine.compactor)
                self.assertTrue(engine.session_manager.save_session_state("empty-draft", [], {}))
                self.assertTrue(engine.load_session("empty-draft"))
                self.assertEqual(engine.session_id, "empty-draft")
                self.assertEqual(engine.messages, [])
                from xuloom.features.plans import get_plan_file_path
                self.assertEqual(engine.plan_manager.plan_file_path, get_plan_file_path("empty-draft"))
                await previous_client.close()
                await previous_compactor.client.close()
                engine.team_manager.create_team("test", engine.agent_id)
                tool = engine.available_tools["agent_tool"]
                engine.messages.append({"role": "user", "content": "PARENT_ONLY: launch ten subagents"})
                engine._sync_message_context()
                child_models, child_tools, child_teams = [], [], []
                gate = asyncio.Event()
                async def child_run(child, user_input, sys_print_callback):
                    self.assertIs(child.permission_manager.approval_policy, engine.permission_manager.approval_policy)
                    self.assertEqual(child.messages, [])
                    self.assertEqual(user_input, "Review")
                    self.assertIn("not the parent coordinator", child.tool_context["agent_system_prompt"])
                    child_models.append(child.primary_model)
                    child_tools.append(set(child.available_tools))
                    child_teams.append(child.team_manager)
                    await gate.wait()
                    return RunResult("review complete", "completed", True)
                with patch.object(QueryEngine, "run", child_run):
                    response = await tool.execute(prompt="Review", subagent_type="explore", model="override-model", run_in_background=True)
                    self.assertIn("Background subagent accepted", response)
                    await asyncio.sleep(0)
                    self.assertEqual(child_models, ["override-model"])
                    self.assertNotIn("file_write_tool", child_tools[0])
                    self.assertIs(child_teams[0], engine.team_manager)
                    self.assertEqual(len(engine.team_manager.get_non_lead_members()), 1)
                    gate.set()
                    manager = engine.tool_context["subagent_tasks"]
                    await asyncio.gather(*manager.tasks.values())
                    self.assertEqual(engine.subagent_registry[-1]["status"], "completed")
                    self.assertEqual(engine.subagent_registry[-1]["result"], "review complete")
                    self.assertFalse(engine.team_manager.get_non_lead_members()[0].is_active)
                engine.todo_manager.write([{"id": "parent", "content": "Parent task", "status": "pending"}])
                parent_todos = engine.todo_manager.export()
                async def independent_run(child, user_input, sys_print_callback):
                    self.assertEqual(child.messages, [])
                    self.assertEqual(user_input, "你好")
                    self.assertEqual(child.todo_manager.export(), [])
                    self.assertNotEqual(child.plan_manager.plan_file_path, engine.plan_manager.plan_file_path)
                    child.todo_manager.write([{"id": "child", "content": "Child task", "status": "completed"}])
                    child.plan_manager.set_mode("plan")
                    role = child.tool_context["agent_system_prompt"]
                    self.assertEqual("PARENT_ONLY" in role, expected_reference)
                    self.assertNotIn("agent_tool", child.available_tools)
                    return RunResult("你好", "completed", True)
                with patch.object(QueryEngine, "run", independent_run):
                    for expected_reference in [False, True]:
                        await tool.execute(prompt="你好", subagent_type="general-purpose", inherit_context=expected_reference)
                        self.assertEqual(engine.todo_manager.export(), parent_todos)
                        self.assertEqual(engine.get_mode(), "normal")
                await engine.client.close()
                await engine.compactor.client.close()
                engine.session_manager.release_writer()
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
