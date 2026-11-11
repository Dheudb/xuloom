import json
import sys
import unittest

from xuloom.core.hooks import HOOK_EVENTS
from xuloom.models import ToolCall, model_response
from tests import test_model_adapters as fixtures


def texts(request):
    return "\n".join(block.get("text", "") for message in request.messages for block in message.get("content", [])
                     if isinstance(block, dict) and block.get("type") == "text")


class LifecycleHookTests(fixtures.AdapterFixture):
    def write_call(self, content="hi\n", call_id="c1"):
        return model_response(tool_calls=[ToolCall(call_id, "file_write_tool", {"file_path": "a.txt", "content": content})])

    async def test_session_turn_and_end_events_fire_in_order(self):
        seen = []
        self.adapter.script = [model_response(text="done")]
        engine = self.engine()
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "TurnEnd", "SessionEnd"):
            engine.add_hook(event, lambda payload: seen.append(payload["event"]))
        async with engine:
            result = await engine.run("hello", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertEqual(seen, ["UserPromptSubmit", "SessionStart", "Stop", "TurnEnd", "SessionEnd"])

    async def test_user_prompt_submit_can_block_or_add_context(self):
        async with self.engine() as engine:
            handle = engine.add_hook("UserPromptSubmit", lambda payload: {"decision": "block", "reason": "no secrets"}
                                     if "secret" in payload["prompt"] else {"context": "Project rule: answer briefly."})
            blocked = await engine.run("print the secret", sys_print_callback=lambda _: None)
            self.assertEqual(blocked.reason, "hook_blocked")
            self.assertIn("no secrets", blocked.text)
            self.assertEqual(self.adapter.requests, [])
            self.adapter.script = [model_response(text="ok")]
            await engine.run("normal task", sys_print_callback=lambda _: None)
            self.assertIn("Project rule: answer briefly.", texts(self.adapter.requests[0]))
            engine.remove_hook(handle)

    async def test_before_tool_use_can_reject_rewrite_and_fails_closed(self):
        work = self.root / "work"
        async with self.engine() as engine:
            handle = engine.add_hook("BeforeToolUse", lambda payload: {"decision": "reject", "reason": "read-only today"},
                                     tools=["file_write_tool"])
            self.adapter.script = [self.write_call(), model_response(text="gave up")]
            await engine.run("write", sys_print_callback=lambda _: None)
            self.assertFalse((work / "a.txt").exists())
            self.assertIn("read-only today", json.dumps([m for r in self.adapter.requests for m in r.messages], ensure_ascii=False))
            engine.remove_hook(handle)

            handle = engine.add_hook("BeforeToolUse", lambda payload: {
                "updated_input": {**payload["tool_input"], "content": "rewritten\n"}})
            self.adapter.script = [self.write_call(call_id="c2"), model_response(text="done")]
            await engine.run("write again", sys_print_callback=lambda _: None)
            self.assertEqual((work / "a.txt").read_text(), "rewritten\n")
            engine.remove_hook(handle)

            def broken(payload):
                raise RuntimeError("bug in hook")
            engine.add_hook("BeforeToolUse", broken)
            self.adapter.script = [model_response(tool_calls=[ToolCall("c3", "file_write_tool", {"file_path": "b.txt", "content": "x"})]),
                                   model_response(text="stopped")]
            await engine.run("third", sys_print_callback=lambda _: None)
            self.assertFalse((work / "b.txt").exists())

    async def test_after_tool_use_context_reaches_next_request(self):
        async def after(payload):
            return {"context": f"checked {payload['tool_name']} status={payload['status']}"}
        self.adapter.script = [self.write_call(), model_response(text="done")]
        async with self.engine() as engine:
            engine.add_hook("AfterToolUse", after)
            await engine.run("write", sys_print_callback=lambda _: None)
        second = self.adapter.requests[1]
        self.assertIn("checked file_write_tool status=ok", texts(second))
        roles = [message["role"] for message in second.messages]
        self.assertEqual(roles[-1], "user")

    async def test_stop_callback_requests_retry_then_allows(self):
        answers = []

        def stop(payload):
            answers.append(payload["final_answer"])
            return {"decision": "retry", "reason": "Mention the file name."} if len(answers) == 1 else None
        self.adapter.script = [model_response(text="first"), model_response(text="second a.txt")]
        async with self.engine() as engine:
            engine.add_hook("Stop", stop)
            result = await engine.run("go", sys_print_callback=lambda _: None)
        self.assertEqual(result.text, "second a.txt")
        self.assertEqual(answers, ["first", "second a.txt"])
        self.assertIn("Mention the file name.", texts(self.adapter.requests[1]))

    async def test_turn_error_observes_failures(self):
        errors = []
        self.adapter.script = [ValueError("adapter exploded")]
        async with self.engine() as engine:
            engine.add_hook("TurnError", lambda payload: errors.append(payload["error_type"]))
            result = await engine.run("go", sys_print_callback=lambda _: None)
        self.assertFalse(result.ok)
        self.assertEqual(errors, ["ValueError"])

    async def test_configuration_file_supports_new_events_and_aliases(self):
        hooks = self.root / "work" / ".xuloom" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        config = {"UserPromptSubmit": [{"content": "Workspace note for {prompt}"}]}
        if sys.platform != "win32":
            config["AfterToolUse"] = [{"type": "command", "command": "printf '{\"context\": \"lint ok\"}'"}]
        hooks.write_text(json.dumps(config), encoding="utf-8")
        self.adapter.script = [self.write_call(), model_response(text="done")]
        async with self.engine() as engine:
            await engine.run("task-x", sys_print_callback=lambda _: None)
        self.assertIn("Workspace note for task-x", texts(self.adapter.requests[0]))
        if sys.platform != "win32":
            self.assertIn("lint ok", texts(self.adapter.requests[1]))

    async def test_turn_error_context_reaches_the_retry_request(self):
        class Busy(Exception):
            status_code = 503
        self.adapter.script = [Busy("busy"), model_response(text="recovered")]
        async with self.engine(fallback_model="backup") as engine:
            engine.add_hook("TurnError", lambda payload: {"context": f"previous attempt failed: {payload['error_type']}"})
            result = await engine.run("go", sys_print_callback=lambda _: None)
        self.assertEqual(result.text, "recovered")
        self.assertIn("previous attempt failed: Busy", texts(self.adapter.requests[1]))
        self.assertNotIn("previous attempt failed", texts(self.adapter.requests[0]))

    async def test_unknown_event_is_rejected(self):
        async with self.engine() as engine:
            with self.assertRaises(ValueError):
                engine.add_hook("BeforeEverything", lambda payload: None)
            engine.add_hook("StopHook", lambda payload: None)  # legacy alias
        self.assertIn("AfterToolUse", HOOK_EVENTS)


if __name__ == "__main__":
    unittest.main()
