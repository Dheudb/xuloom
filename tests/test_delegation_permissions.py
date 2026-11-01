"""Delegation cannot escape live parent restrictions or the selected workspace policy."""
import asyncio
import json
import unittest
from unittest.mock import patch

from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult
from xuloom.core.storage import atomic_json
if __package__:
    from . import test_agent_lifecycle as lifecycle
else:
    import test_agent_lifecycle as lifecycle


class DelegationPermissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.LifecycleTests.asyncSetUp
    asyncTearDown = lifecycle.LifecycleTests.asyncTearDown
    make_engine = lifecycle.LifecycleTests.make_engine

    async def call(self, engine, name, values):
        engine.require_writer()
        return await engine._execute_tool_wrapper({"id": "probe-" + str(len(engine.recent_tool_activity)), "name": name, "input": values}, lambda _: None)

    async def child(self, run):
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            return json.loads(await self.tool.execute(prompt="independent task", response_format="json", run_in_background=False))["agents"][0]

    async def test_parent_session_deny_prevents_actual_child_write(self):
        target = self.root / "must-not-exist.txt"
        self.engine.permission_manager.always_deny_tools.add("file_write_tool")
        observed = []
        async def run(child, **kwargs):
            observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            return RunResult("reported", "completed", True)
        await self.child(run)
        self.assertTrue(observed[0]["is_error"], observed)
        self.assertFalse(target.exists(), "A child must not bypass the parent's remembered refusal")

    async def test_workspace_policy_uses_engine_cwd_not_process_cwd(self):
        atomic_json(self.root / ".xuloom/permissions.json", {"alwaysDeny": ["file_write_tool"]})
        self.engine.permission_manager.approval_policy.set_mode("full")
        target = self.root / "workspace-denied.txt"
        result = await self.call(self.engine, "file_write_tool", {"file_path": str(target), "content": "DENIED"})
        self.assertTrue(result["is_error"], result); self.assertFalse(target.exists())

    async def test_parent_always_ask_applies_to_child_reads_even_with_full_access(self):
        target = self.root / "private.txt"; target.write_text("PRIVATE_EVIDENCE", encoding="utf-8")
        self.engine.permission_manager.always_ask_tools.add("file_read_tool")
        requests, observed = [], []
        async def approval(request): requests.append(request); return "deny"
        self.engine.tool_context["permission_handler"] = approval
        async def run(child, **kwargs):
            observed.append(await self.call(child, "file_read_tool", {"file_path": str(target)}))
            return RunResult("reported", "completed", True)
        await self.child(run)
        self.assertEqual(len(requests), 1); self.assertTrue(observed[0]["is_error"])
        self.assertNotIn("PRIVATE_EVIDENCE", observed[0]["content"])

    async def test_parent_restrictions_added_after_child_started_are_live(self):
        ready, gate = asyncio.Event(), asyncio.Event(); target = self.root / "late-denied.txt"; observed = []
        async def run(child, **kwargs):
            ready.set(); await gate.wait()
            observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            return RunResult("reported", "completed", True)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            task = asyncio.create_task(self.tool.execute(prompt="wait", run_in_background=False))
            await asyncio.wait_for(ready.wait(), 5)
            self.engine.permission_manager.always_deny_tools.add("file_write_tool"); gate.set(); await task
        self.assertTrue(observed[0]["is_error"]); self.assertFalse(target.exists())

    async def test_parent_plan_mode_changed_during_worker_blocks_project_writes(self):
        ready, gate = asyncio.Event(), asyncio.Event(); target = self.root / "plan-denied.txt"; observed = []
        async def run(child, **kwargs):
            ready.set(); await gate.wait()
            observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            from xuloom.features.plans import get_plan_file_path
            own_plan = get_plan_file_path(child.session_id)
            observed.append(await self.call(child, "file_write_tool", {"file_path": own_plan, "content": "ALLOWED PLAN"}))
            return RunResult("reported", "completed", True)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            task = asyncio.create_task(self.tool.execute(prompt="plan safely", run_in_background=False))
            await asyncio.wait_for(ready.wait(), 5); self.engine.plan_manager.set_mode("plan"); gate.set(); await task
        self.assertTrue(observed[0]["is_error"]); self.assertFalse(target.exists())
        self.assertFalse(observed[1]["is_error"], observed)

    async def test_parent_session_allow_never_becomes_a_child_approval(self):
        target = self.root / "no-inherited-allow.txt"; requests, observed = [], []
        self.engine.permission_manager.always_allow_tools.add("file_write_tool")
        async def approval(request): requests.append(request); return "deny"
        self.engine.tool_context["permission_handler"] = approval
        async def run(child, **kwargs):
            observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            return RunResult("reported", "completed", True)
        with patch.object(QueryEngine, "run", run):
            await self.tool.execute(prompt="permission isolation", run_in_background=False)
        self.assertEqual(len(requests), 1); self.assertTrue(observed[0]["is_error"]); self.assertFalse(target.exists())

    async def test_worker_remembers_its_own_refusal_across_followup(self):
        target = self.root / "never.txt"; results = []; calls = 0
        async def approval(request): return "never"
        self.engine.tool_context["permission_handler"] = approval
        async def run(child, **kwargs):
            nonlocal calls
            calls += 1
            child.permission_manager.approval_policy.set_mode("ask" if calls == 1 else "full")
            results.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            return RunResult("reported", "completed", True)
        with patch.object(QueryEngine, "run", run):
            first = json.loads(await self.tool.execute(prompt="first", response_format="json", run_in_background=False))["agents"][0]
            await self.tool.execute(action="followup", agent_id=first["agent_id"], prompt="try again", run_in_background=False)
        self.assertEqual(len(results), 2); self.assertTrue(all(row["is_error"] for row in results)); self.assertFalse(target.exists())

    async def test_custom_parent_policy_is_rechecked_and_unreadable_is_denied(self):
        from pathlib import Path
        parent_policy = self.root / "host-policy.json"
        atomic_json(parent_policy, {"alwaysDeny": ["file_write_tool"]})
        self.engine.permission_manager.config_path = str(parent_policy)
        target = self.root / "host-denied.txt"; observed = []
        readable = self.root / "readable.txt"; readable.write_text("RECOVERED_READ", encoding="utf-8")
        async def run(child, **kwargs):
            observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            Path(self.engine.permission_manager.config_path).write_text("{broken", encoding="utf-8")
            for _ in range(4):
                observed.append(await self.call(child, "file_read_tool", {"file_path": str(readable)}))
            atomic_json(parent_policy, {})
            restored = await self.call(child, "file_read_tool", {"file_path": str(readable)})
            self.assertFalse(restored["is_error"], restored)
            self.assertIn("RECOVERED_READ", restored["content"])
            return RunResult("reported", "completed", True)
        await self.child(run)
        self.assertTrue(all(row["is_error"] for row in observed)); self.assertFalse(target.exists())

    async def test_parent_denial_applies_to_trusted_tool_alias(self):
        from xuloom.agents.agent_tool import SpawnAgentTool
        observed = []
        async def run(child, **kwargs):
            self.engine.permission_manager.always_deny_tools.add("agent_tool")
            observed.append(await child.permission_manager.authorize(SpawnAgentTool(), {"prompt": "must not launch"}))
            return RunResult("reported", "completed", True)
        await self.child(run)
        self.assertEqual(observed[0].behavior, "deny")
        self.assertEqual(observed[0].source, "delegated_session_rule")

    async def test_restrictions_survive_parent_restart_and_same_worker_followup(self):
        target = self.root / "after-restart.txt"; observed = []; calls = 0
        async def run(child, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                child.permission_manager.always_deny_tools.add("file_write_tool")
            else:
                observed.append(await self.call(child, "file_write_tool", {"file_path": str(target), "content": "DENIED"}))
            return RunResult("reported", "completed", True)
        with patch.object(QueryEngine, "run", run):
            first = json.loads(await self.tool.execute(prompt="first", response_format="json", run_in_background=False))["agents"][0]
            self.engine.persist_session_state(); self.engine.session_manager.release_writer()
            restored = self.make_engine()
            self.assertTrue(restored.load_session(self.engine.session_id))
            restored.permission_manager.approval_policy.set_mode("full")
            await restored.available_tools["agent_tool"].execute(action="followup", agent_id=first["agent_id"],
                prompt="continue after restart", run_in_background=False)
        self.assertEqual(calls, 2); self.assertTrue(observed[0]["is_error"]); self.assertFalse(target.exists())

    async def test_grandparent_restrictions_and_invalid_ancestry_fail_closed(self):
        from types import SimpleNamespace
        from xuloom.security.permissions import PermissionManager, ApprovalPolicy
        parent = PermissionManager(config_path=str(self.root / "missing-parent.json"), approval_policy=ApprovalPolicy("full"))
        child = PermissionManager(config_path=str(self.root / "missing-child.json"), approval_policy=parent.approval_policy)
        parent.restriction_parent = self.engine.permission_manager
        child.restriction_parent = parent
        self.engine.permission_manager.always_deny_tools.add("fixture")
        tool = SimpleNamespace(name="fixture", is_read_only=True)
        self.assertEqual((await child.authorize(tool, {})).behavior, "deny")
        self.engine.permission_manager.always_deny_tools.clear()
        self.engine.permission_manager.restriction_parent = child
        self.assertEqual((await child.authorize(tool, {})).source, "delegation_policy_error")
        child.restriction_parent = object()
        self.assertEqual((await child.authorize(tool, {})).source, "delegation_policy_error")
