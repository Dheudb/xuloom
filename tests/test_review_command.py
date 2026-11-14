import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.features.review import parse, review_request


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=root,
                          check=True, capture_output=True, text=True).stdout.strip()


class ReviewCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(self.temp.name) / "data"),
                                      "XULOOM_CONFIG_DIR": str(Path(self.temp.name) / "config")})
        env.start()
        self.addCleanup(env.stop)

    def commit(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
        git(self.root, "add", name)
        git(self.root, "commit", "-q", "-m", name)
        return git(self.root, "rev-parse", "HEAD")

    def test_parse_separates_target_and_notes(self):
        self.assertEqual(parse(""), ("", ""))
        self.assertEqual(parse(" HEAD~2..HEAD "), ("HEAD~2..HEAD", ""))
        self.assertEqual(parse("-- 重点看并发"), ("", "重点看并发"))
        self.assertEqual(parse("main...HEAD -- check errors"), ("main...HEAD", "check errors"))
        with self.assertRaises(ValueError):
            parse("main HEAD")
        with self.assertRaises(ValueError):
            parse("--output=x")

    async def test_current_changes_require_a_dirty_worktree(self):
        self.commit("a.txt", "one\n")
        with self.assertRaisesRegex(ValueError, "No uncommitted changes"):
            await review_request(self.root, "")
        (self.root / "new.txt").write_text("draft\n", encoding="utf-8")
        prompt, options = await review_request(self.root, " -- focus on data loss")
        self.assertEqual(options["selected_skills"], ["code-review"])
        self.assertEqual(options["review_scope"]["kind"], "worktree")
        self.assertIn("uncommitted Git changes", prompt)
        self.assertIn("review_git", prompt)
        self.assertTrue(prompt.endswith("Reviewer notes: focus on data loss"))

    async def test_commit_and_range_are_pinned_to_hashes(self):
        first = self.commit("a.txt", "one\n")
        second = self.commit("a.txt", "two\n")
        prompt, _ = await review_request(self.root, "HEAD")
        self.assertIn(second, prompt)
        prompt, options = await review_request(self.root, "HEAD~1..")
        self.assertIn(f"{first}..{second}", prompt)
        self.assertEqual(options["review_scope"]["base"], first)
        prompt, options = await review_request(self.root, f"{first}...HEAD")
        self.assertIn(f"{first}...{second}", prompt)
        self.assertEqual(options["review_scope"]["base"], first)
        _, options = await review_request(self.root, "HEAD..HEAD~1")
        self.assertEqual(options["review_scope"]["head"], first)

    async def test_invalid_targets_fail_before_any_model_request(self):
        self.commit("a.txt", "one\n")
        for target in ("missing-branch", "--output=x", "HEAD..HEAD"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await review_request(self.root, target)

    async def test_review_turn_loads_the_skill_and_keeps_the_typed_command(self):
        from xuloom import QueryEngine
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response
        commit = self.commit("a.txt", "one\n")
        prompt, options = await review_request(self.root, "HEAD")
        model = ScriptedModel(default=model_response(text="No findings."))
        with model:
            async with QueryEngine(model.config(cwd=str(self.root))) as engine:
                events = [event async for event in engine.submit_message(
                    prompt, sys_print_callback=lambda _: None, display_text="/review HEAD", **options)]
        sent = str(model.requests[0].messages[-1]["content"])
        self.assertIn(commit, sent)
        self.assertIn("review_git", sent)
        self.assertIn("<selected-skills>", sent)
        self.assertIn("Skill: code-review", sent)
        self.assertIn({"type": "skill_loaded", "name": "code-review"},
                      [{key: event.get(key) for key in ("type", "name")} for event in events if event.get("type") == "skill_loaded"])

    async def test_unrelated_three_dot_range_is_rejected(self):
        first = self.commit("a.txt", "one")
        git(self.root, "switch", "--orphan", "unrelated")
        second = self.commit("b.txt", "two")
        with self.assertRaisesRegex(ValueError, "no common ancestor"):
            await review_request(self.root, f"{first}...{second}")
        _, options = await review_request(self.root, f"{first}..{second}")
        self.assertEqual(options["review_scope"]["base"], first)

    async def test_review_blocks_writes_commands_delegation_and_hooks_in_full_access(self):
        import json
        import sys
        from xuloom import ApprovalPolicy, QueryEngine
        from xuloom.models.adapter import ToolCall
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response

        self.commit("a.txt", "previous\n")
        commit = self.commit("a.txt", "review this line\n")
        (self.root / "unrelated.txt").write_text("uncommitted", encoding="utf-8")
        forbidden = self.root / "forbidden.txt"
        hook_marker = self.root / "hook-ran.txt"
        calls = [
            ToolCall("write", "file_write_tool", {"file_path": str(forbidden), "content": "blocked"}),
            ToolCall("command", "command_tool", {"action": "start", "argv": [sys.executable, "-c", f"open({str(forbidden)!r}, 'w').close()"]}),
            ToolCall("worker", "spawn_agent", {"prompt": "write a file", "description": "blocked"}),
            ToolCall("diff", "review_git", {"action": "diff"}),
        ]
        model = ScriptedModel([model_response(tool_calls=calls), model_response(text="Review complete."), model_response(
            tool_calls=[ToolCall("normal", "file_write_tool", {"file_path": str(forbidden), "content": "allowed"})]),
            model_response(text="Written.")])
        prompt, options = await review_request(self.root, commit)
        with model:
            config = model.config(cwd=str(self.root), approval_policy=ApprovalPolicy("full"), collaboration_mode="ultra")
            async with QueryEngine(config) as engine:
                hooks = self.root / "fixture-hooks.json"
                hooks.write_text(json.dumps({"Stop": [{"type": "command",
                    "command": f'"{sys.executable}" -c "from pathlib import Path; Path({str(hook_marker)!r}).touch()"'}]}),
                    encoding="utf-8")
                engine.hook_manager.config_path = str(hooks)
                callback_events = []
                engine.add_hook("TurnEnd", callback_events.append)
                tool_events = []
                engine.set_event_handler(tool_events.append)
                events = [event async for event in engine.submit_message(
                    prompt, display_text="/review HEAD", sys_print_callback=lambda _: None, **options)]
                self.assertTrue(events[-1]["result"]["ok"])
                results = {event["tool_use_id"]: event for event in [*events, *tool_events] if event["type"] == "tool_result"}
                for blocked in ("write", "command", "worker"):
                    self.assertIn("outside the read-only review scope", results[blocked]["result"])
                self.assertIn("+review this line", results["diff"]["result"])
                self.assertNotIn("unrelated.txt", results["diff"]["result"])
                self.assertFalse(forbidden.exists())
                self.assertFalse(hook_marker.exists())
                self.assertEqual(callback_events, [])
                self.assertEqual({tool["name"] for tool in model.requests[0].tools},
                                 {"file_read_tool", "grep_tool", "glob_tool", "review_git"})
                engine.set_collaboration_mode("solo")
                await engine.run("Write the fixture", sys_print_callback=lambda _: None)
                self.assertEqual(forbidden.read_text(encoding="utf-8"), "allowed")
                self.assertEqual(len(callback_events), 1)
                self.assertTrue(hook_marker.exists())

    async def test_review_keeps_before_tool_rejections_but_ignores_rewrites_and_context(self):
        import json
        from xuloom import ApprovalPolicy, QueryEngine
        from xuloom.models.adapter import ToolCall
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response

        commit = self.commit("a.txt", "review this line\n")
        secret = self.root / "secret.env"
        secret.write_text("TOKEN=do-not-leak", encoding="utf-8")
        hooks = self.root / "fixture-hooks.json"
        hooks.write_text(json.dumps({"BeforeToolUse": [
            {"tools": ["file_read_tool"], "behavior": "reject", "content": "secrets are off limits"},
            {"tools": ["review_git"], "content": "CONFIG-INJECTED {broken", "input_patch": {"action": "status"}}],
            "Stop": False, "PostSampling": 7}),
            encoding="utf-8")

        def callback(payload):
            if payload["tool_name"] == "glob_tool":
                return {"decision": "reject", "reason": "globbing is disabled"}
            return {"context": "CALLBACK-INJECTED", "updated_input": {"action": "status"}}

        model = ScriptedModel([model_response(tool_calls=[
            ToolCall("read", "file_read_tool", {"file_path": str(secret)}),
            ToolCall("glob", "glob_tool", {"pattern": "*.env"}),
            ToolCall("diff", "review_git", {"action": "diff"})]), model_response(text="Review complete.")])
        prompt, options = await review_request(self.root, commit)
        with model:
            config = model.config(cwd=str(self.root), approval_policy=ApprovalPolicy("full"))
            async with QueryEngine(config) as engine:
                engine.hook_manager.config_path = str(hooks)
                engine.add_hook("BeforeToolUse", callback)
                tool_events = []
                engine.set_event_handler(tool_events.append)
                events = [event async for event in engine.submit_message(
                    prompt, display_text="/review HEAD", sys_print_callback=lambda _: None, **options)]
        self.assertTrue(events[-1]["result"]["ok"])
        results = {event["tool_use_id"]: str(event["result"]) for event in [*events, *tool_events]
                   if event["type"] == "tool_result"}
        self.assertIn("secrets are off limits", results["read"])
        self.assertNotIn("do-not-leak", results["read"])
        self.assertIn("globbing is disabled", results["glob"])
        self.assertIn("+review this line", results["diff"])
        self.assertNotIn("INJECTED", results["diff"])
        self.assertNotIn("INJECTED", str(model.requests[-1].messages))

    def test_review_rejects_invalid_before_tool_configuration(self):
        from xuloom.core.hooks import HookManager, suppress_review_hooks

        path = self.root / "fixture-hooks.json"
        manager = HookManager(path, cwd=self.root)
        token = suppress_review_hooks.set(True)
        try:
            for malformed in ("[]", '{"BeforeToolUse": {}}', '{"BeforeToolUse": false}'):
                with self.subTest(config=malformed):
                    path.write_text(malformed, encoding="utf-8")
                    decision = manager.evaluate_before_tool_hooks({"tool_name": "review_git"})
                    self.assertEqual(decision.behavior, "reject")
        finally:
            suppress_review_hooks.reset(token)

    async def test_review_scope_survives_resuming_with_applied_steering(self):
        from xuloom import ApprovalPolicy, QueryEngine
        from xuloom.core.steering import consume
        from xuloom.models.adapter import ToolCall
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response

        commit = self.commit("a.txt", "review target")
        started = asyncio.Event()

        async def stop_point(request):
            started.set()
            await engine.abort_event.wait()
            return model_response(text="partial")

        output = self.root / "forbidden.txt"
        model = ScriptedModel([stop_point, model_response(tool_calls=[
            ToolCall("forbidden", "file_write_tool", {"file_path": str(output), "content": "no"}),
            ToolCall("review", "review_git", {"action": "diff"})]), model_response(text="Review complete.")])
        prompt, options = await review_request(self.root, commit)
        with model:
            config = model.config(cwd=str(self.root), approval_policy=ApprovalPolicy("full"), collaboration_mode="solo")
            async with QueryEngine(config) as engine:
                async def turn(text, **kwargs):
                    return [event async for event in engine.submit_message(text, sys_print_callback=lambda _: None, **kwargs)]
                running = asyncio.create_task(turn(prompt, **options))
                await asyncio.wait_for(started.wait(), 5)
                engine.queue_input("Inspect this commit carefully")
                await consume(engine)
                await engine.request_abort()
                await running
                session = engine.session_id
            async with QueryEngine(config) as engine:
                self.assertTrue(engine.load_session(session))
                tool_events = []
                engine.set_event_handler(tool_events.append)
                resumed = await turn("", resume=True)
        self.assertTrue(resumed[-1]["result"]["ok"])
        self.assertFalse(output.exists())
        self.assertIn("review target", next(event["result"] for event in tool_events
                                          if event.get("tool_use_id") == "review" and event["type"] == "tool_result"))

    async def test_outside_a_repository_reports_the_reason(self):
        with tempfile.TemporaryDirectory() as plain:
            with self.assertRaisesRegex(ValueError, "Git"):
                await review_request(plain, "")


if __name__ == "__main__":
    unittest.main()
