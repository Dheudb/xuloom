import json
import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

import test_tool_recovery as recovery
from xuloom.agents.registry import AgentRegistry
from xuloom.context.context import ContextBuilder
from xuloom.skills.registry import SkillRegistry


class NeutralFrameworkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    async def test_default_prompt_has_no_repository_probe_or_coding_policy(self):
        builder = ContextBuilder(str(self.root))
        with patch("git.Repo", side_effect=AssertionError("No default Git probe")):
            prompt = builder.generate_system_prompt()
        for old in ("primarily request", "3+ file", "helping the user with programming", "independent adversarial verification", "Explore the codebase"):
            self.assertNotIn(old, prompt)
        self.assertIn("general-purpose assistant", prompt)
        self.assertIn("not new user authorization", prompt)

    async def test_instruction_sources_are_explicit_and_legacy_is_opt_in(self):
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/CLAUDE.md").write_text("LEGACY_CODE_RULE", encoding="utf-8")
        (home / ".xuloom").mkdir()
        (home / ".xuloom/instructions.md").write_text("USER_DEFAULT", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("WORKSPACE_RULE", encoding="utf-8")
        builder = ContextBuilder(str(self.root))
        prompt = builder.generate_system_prompt()
        self.assertIn("USER_DEFAULT", prompt)
        self.assertIn("WORKSPACE_RULE", prompt)
        self.assertNotIn("LEGACY_CODE_RULE", prompt)
        SkillRegistry(self.root).update(legacy_instructions=True)
        self.assertIn("LEGACY_CODE_RULE", builder.generate_system_prompt())

    async def test_conversation_completes_without_tools_or_workers(self):
        for text in ("你好", "解释一下什么是光合作用", "把这句话写得更简洁"):
            self.fixture(["DIRECT_ANSWER"])
            self.requests.clear()
            self.assertFalse(hasattr(self.engine.attachment_collector, "get_changed_files_from_git"))
            result = await self.engine.run(text, sys_print_callback=lambda _: None)
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "DIRECT_ANSWER")
            self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.engine.recent_tool_activity, [])
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_plan_guidance_has_no_mandatory_software_exploration_or_agent_count(self):
        plan = self.engine.attachment_collector._build_plan_mode_reminder("plan", "", 1, plan_file_path="plan.md")
        for text in ("codebase", "at least 1 Plan agent", "up to 3 Explore", "phase is MANDATORY"):
            self.assertNotIn(text, plan["content"])
        self.assertIn("checks appropriate to the domain", plan["content"])
        self.assertIn("exit_plan_mode", plan["content"])

    async def test_discovering_specialized_tools_does_not_grant_execution_permission(self):
        specialized = {"repl_tool", "sandbox_tool", "notebook_edit_tool", "team_create_tool"}
        self.assertTrue(specialized.isdisjoint(self.engine.available_tools))
        self.assertTrue(specialized.issubset(self.engine.latent_tools))
        self.engine.permission_manager.always_deny_tools.add("repl_tool")
        discovered = await self.engine._execute_tool_wrapper({"id": "discover-repl", "name": "tool_search_tool", "input": {"query": "repl python computation", "activate": ["repl_tool"]}}, lambda _: None)
        self.assertFalse(discovered["is_error"])
        self.assertIn("repl_tool", self.engine.available_tools)
        marker = self.root / "forbidden-repl-effect"
        denied = await self.engine._execute_tool_wrapper({"id": "denied-repl", "name": "repl_tool", "input": {"language": "python", "code": "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('bad')"}}, lambda _: None)
        self.assertTrue(denied["is_error"])
        self.assertFalse(marker.exists())

    async def test_three_documents_and_python_artifact_do_not_execute_coding_checks(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        marker = self.root / "unrequested-execution"
        tasks = [("file_write_tool", json.dumps({"file_path": str(self.root / f"draft-{i}.md"), "content": "Writing task"})) for i in range(3)]
        tasks.append(("file_write_tool", json.dumps({"file_path": str(self.root / "run_report.py"), "content": "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"})))
        self.fixture(tasks + ["ARTIFACTS_SAVED"])
        result = await self.engine.run("Save the requested artifacts without executing them", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(len(self.requests), 5)
        self.assertFalse(marker.exists())
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertFalse(self.engine.verification_manager.enabled)

    async def test_programming_role_is_optional_native_skill_extension(self):
        role = AgentRegistry(self.root).resolve("software-engineer")
        self.assertEqual(role["skills"], ["code-development"])
        self.assertIn("executable checks", SkillRegistry(self.root).read("code-development"))
        generic = AgentRegistry(self.root).resolve("general-purpose")
        self.assertNotIn("searching for code", generic["system_prompt"].lower())
        self.assertIn("Respond directly", generic["system_prompt"])

    async def test_cli_and_streamed_selections_load_the_same_native_skill_once(self):
        for streamed in (False, True):
            self.fixture(["EXTENSION_LOADED"])
            self.requests.clear()
            if streamed:
                events = [event async for event in self.engine.submit_message("Use this optional workflow", selected_skills=["code-development"])]
                self.assertEqual(sum(event["type"] == "skill_loaded" for event in events), 1)
                self.assertTrue(events[-1]["result"]["ok"])
            else:
                result = await self.engine.run("$code-development Use this optional workflow", selected_skills=["code-development"], sys_print_callback=lambda _: None)
                self.assertTrue(result.ok)
            latest = next(message for message in reversed(self.requests[0]["messages"]) if message["role"] == "user")
            serialized = json.dumps(latest, ensure_ascii=False)
            self.assertEqual(serialized.count("Skill: code-development"), 1)
            self.assertIn("executable checks", serialized)
            human = next(message for message in reversed(self.engine.messages) if message.get("_origin") == "human")
            self.assertNotIn("selected-skills", human["_display_text"])

    async def test_instruction_preview_marks_omitted_text_and_keeps_source_paths(self):
        (self.root / "AGENTS.md").write_text("x" * 7000 + "END_OF_RULES", encoding="utf-8")
        prompt = ContextBuilder(str(self.root)).generate_system_prompt()
        self.assertIn("Truncated: showing 6000 of 7012 characters", prompt)
        self.assertIn(str(self.root / "AGENTS.md"), prompt)
        self.assertIn("Read the complete file", prompt)

    async def test_overlapping_programmatic_turn_is_rejected_without_replacing_the_owner(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            from xuloom.core.run_result import RunResult
            entered.set()
            await finish.wait()
            return RunResult("owner answer", "completed", True)
        with patch.object(self.engine, "_run_loop", side_effect=held):
            owner = asyncio.create_task(self.engine.run("owner"))
            await entered.wait()
            result = await self.engine.run("overlapping")
            self.assertEqual(result.reason, "session_busy")
            events = [event async for event in self.engine.submit_message("also overlapping")]
            self.assertEqual(events[-1]["result"]["reason"], "session_busy")
            self.assertIs(self.engine.active_run_task, owner)
            finish.set()
            self.assertTrue((await owner).ok)

    async def test_large_skill_result_uses_complete_output_archive(self):
        directory = self.root / ".xuloom/skills/large-instructions"
        directory.mkdir(parents=True)
        directory.joinpath("SKILL.md").write_text("---\nname: large-instructions\ndescription: Archive test.\n---\n" + "Long instruction.\n" * 300 + "TAIL_PROOF", encoding="utf-8")
        receipt = await self.engine._execute_tool_wrapper({"id": "archive-skill", "name": "skill_tool", "input": {"action": "read", "skill_name": "large-instructions", "max_chars": 1000}}, lambda _: None)
        self.assertFalse(receipt["is_error"])
        self.assertIn("Reference path:", receipt["content"])
        spills = [row for row in self.engine.content_replacement_manager.registry.values() if row["tool_use_id"] == "archive-skill"]
        self.assertEqual(len(spills), 1)
        self.assertIn("TAIL_PROOF", Path(spills[0]["path"]).read_text(encoding="utf-8"))
