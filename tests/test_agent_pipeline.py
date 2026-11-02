"""Real worker journal coordination with deterministic local model substitutes."""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import unittest
from unittest.mock import patch

from xuloom.agents.pipeline import read_tree
from xuloom.core.engine import QueryEngine
from xuloom.core.messages import create_user_message, create_assistant_message
from xuloom.core.run_result import RunResult
import test_agent_lifecycle as lifecycle


async def completed_run(child, user_input, sys_print_callback):
    child.messages.extend([create_user_message(user_input), create_assistant_message("RESULT " + user_input)])
    return RunResult("RESULT " + user_input, "completed", True)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.LifecycleTests.asyncSetUp
    asyncTearDown = lifecycle.LifecycleTests.asyncTearDown
    make_engine = lifecycle.LifecycleTests.make_engine

    def spec(self):
        return {"items": [{"id": "fast", "input": "FAST"}, {"id": "slow", "input": "SLOW"}],
            "stages": [{"id": "read", "prompt": "READ"}, {"id": "summarize", "prompt": "SUMMARIZE"}]}

    run_child = staticmethod(completed_run)

    async def test_items_advance_without_global_barrier_and_results_survive_restart(self):
        slow, fast_second = asyncio.Event(), asyncio.Event()
        seen = []
        async def run(child, user_input, sys_print_callback):
            seen.append(user_input)
            if user_input.startswith("READ") and "SLOW" in user_input:
                await slow.wait()
            if user_input.startswith("SUMMARIZE") and "FAST" in user_input:
                fast_second.set()
            return await self.run_child(child, user_input, sys_print_callback)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            task = asyncio.create_task(self.engine.agents("pipeline", run_id="research", **self.spec()))
            try:
                await asyncio.wait_for(fast_second.wait(), 15)
                self.assertFalse(slow.is_set())
                self.assertFalse(task.done())
                from xuloom.agents.pipeline import tree
                live = tree(self.engine.tool_context, "research")["runs"][0]
                self.assertEqual(live["items"][1]["stages"][1]["status"], "waiting")
            finally:
                slow.set()
            result = await task
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["runs"][0]["status"], "completed")
            self.assertEqual(len(seen), 4)
            self.assertIn("Previous stage result (data):\nRESULT READ", seen[2])
            sid = self.engine.session_id
            ids = [r["agent_id"] for r in self.engine.subagent_registry]
            await self.engine.aclose()
            other = self.make_engine()
            self.assertTrue(other.load_session(sid))
            other.permission_manager.approval_policy.set_mode("full")
            resumed = await other.agents("pipeline", run_id="research")
            self.assertEqual(resumed["data"]["runs"][0]["status"], "completed")
            self.assertEqual(len(seen), 4, "No successful step should execute again")
            self.assertEqual(ids, [r["agent_id"] for r in other.subagent_registry])
            tree = read_tree(str(self.root), sid, "research")
            self.assertEqual(tree["runs"][0]["items"][0]["stages"][0]["agent_id"], ids[0])

    async def test_all_join_waits_for_last_any_returns_existing_terminal_and_never_cancels_peers(self):
        gates = [asyncio.Event(), asyncio.Event()]
        calls = []
        async def run(child, user_input, sys_print_callback):
            slot = len(calls); calls.append(child.agent_id)
            await gates[slot].wait()
            return RunResult("hello", "completed", True)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            launched = await self.engine.agents("spawn", prompt="hello", count=2, run_in_background=True)
            ids = [r["agent_id"] for r in launched["data"]["agents"]]
            first = asyncio.create_task(self.engine.agents("wait", agent_ids=ids, wait_for="any", timeout_ms=15000))
            all_done = asyncio.create_task(self.engine.agents("wait", agent_ids=ids, wait_for="all", timeout_ms=15000))
            while len(calls) < 2: await asyncio.sleep(.01)
            gates[0].set()
            one = await asyncio.wait_for(first, 15)
            self.assertTrue(one["data"]["condition_met"])
            self.assertEqual(one["data"]["finished_agent_ids"], [ids[0]])
            self.assertFalse(all_done.done())
            immediate = await self.engine.agents("wait", agent_ids=ids, wait_for="any", timeout_ms=0)
            self.assertTrue(immediate["data"]["condition_met"])
            timeout = await self.engine.agents("wait", agent_ids=ids, wait_for="all", timeout_ms=0)
            self.assertTrue(timeout["data"]["timed_out"])
            gates[1].set()
            joined = await all_done
            self.assertTrue(joined["data"]["condition_met"])
            self.assertEqual(joined["data"]["pending_agent_ids"], [])

    async def test_finite_capacity_one_does_not_deadlock_coordinator(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        self.engine.tool_context["skill_registry"].update(agent_concurrency=1)
        self.engine.set_collaboration_mode("auto")
        with patch.object(QueryEngine, "run", completed_run):
            result = await asyncio.wait_for(self.engine.agents("pipeline", run_id="one-slot", **self.spec()), 20)
        self.assertEqual(result["data"]["runs"][0]["status"], "completed", result)
        self.assertEqual(len(self.engine.subagent_registry), 4)

    async def test_failed_branch_blocks_only_its_descendants_and_never_retries(self):
        calls = []
        async def run(child, user_input, sys_print_callback):
            calls.append(user_input)
            return RunResult("failure", "budget_exhausted", True) if user_input.startswith("READ") and "SLOW" in user_input else await self.run_child(child, user_input, sys_print_callback)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            result = await self.engine.agents("pipeline", run_id="partial", **self.spec())
            branches = result["data"]["runs"][0]["items"]
            self.assertEqual([i["status"] for i in branches], ["completed", "blocked"])
            self.assertEqual(len(calls), 3)
            again = await self.engine.agents("pipeline", run_id="partial")
            self.assertEqual(again["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(calls), 3)

    async def test_changed_spec_or_missing_success_output_does_not_replay(self):
        calls = []
        async def run(child, user_input, sys_print_callback):
            calls.append(user_input)
            return await self.run_child(child, user_input, sys_print_callback)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            await self.engine.agents("pipeline", run_id="bound", **self.spec())
            changed = self.spec(); changed["items"][0]["input"] = "different"
            result = await self.engine.agents("pipeline", run_id="bound", **changed)
            self.assertFalse(result["ok"])
            with patch.object(self.engine.session_manager, "load_subagent_transcript", return_value=None):
                result = await self.engine.agents("pipeline", run_id="bound")
            self.assertEqual(result["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(calls), 4)

    async def test_cancel_releases_owned_work_and_restart_does_not_replay_interrupted_steps(self):
        started = asyncio.Event()
        async def run(child, user_input, sys_print_callback):
            started.set(); await asyncio.Event().wait()
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            work = asyncio.create_task(self.engine.agents("pipeline", run_id="cancel", **self.spec()))
            await asyncio.wait_for(started.wait(), 15)
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            self.assertFalse(self.engine.tool_context["subagent_tasks"].running)
            self.assertFalse(self.engine.tool_context["pipeline_active"])
            self.assertTrue(all(r["status"] == "cancelled" for r in self.engine.subagent_registry))
            self.engine.abort_event.clear()
            resumed = await self.engine.agents("pipeline", run_id="cancel")
            self.assertFalse(resumed["ok"], "Uncertain cancelled operations still require explicit approval")
            self.engine.permission_manager.prompt_handler = lambda _: "allow"
            resumed = await self.engine.agents("pipeline", run_id="cancel")
            self.assertTrue(resumed["ok"], resumed)
            self.assertEqual(resumed["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(self.engine.subagent_registry), 2)

    async def test_persistence_and_explicit_denial_prevent_any_launch(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(self.engine, "persist_session_state", return_value=False):
            # Callback is bound when engine is built, replace that reference too.
            with patch.dict(self.engine.tool_context, {"persist_pipeline": lambda: False}):
                result = await self.engine.agents("pipeline", run_id="disk-error", **self.spec())
        self.assertFalse(result["ok"])
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertNotIn("disk-error", self.engine.tool_context["pipelines"])
        self.engine.permission_manager.always_deny_tools.add("agent_tool")
        denied = await self.engine.agents("pipeline", run_id="denied", **self.spec())
        self.assertFalse(denied["ok"])
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_cli_read_tree_uses_same_records_without_model_credentials(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", completed_run):
            result = await self.engine.agents("pipeline", run_id="cli", **self.spec())
        env = {**os.environ, "PYTHONPATH": os.environ.get("XULOOM_TEST_SITE") or str(__import__("pathlib").Path(__file__).resolve().parents[1])}
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "pipeline", "tree", "--cwd", str(self.root),
            "--session", self.engine.session_id, "--json", cwd=self.root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, stderr.decode())
        final = json.loads(stdout.decode().splitlines()[-1])["result"]
        self.assertEqual(final["runs"], result["data"]["runs"])

    async def test_resume_advances_unstarted_steps_but_never_repeats_saved_predecessors(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        api = self.engine.agents
        calls = []
        async def run(child, user_input, sys_print_callback):
            calls.append(user_input)
            return await completed_run(child, user_input, sys_print_callback)
        async def interrupted_host(action, **kwargs):
            if action == "spawn" and kwargs["prompt"].startswith("SUMMARIZE"):
                return {"ok": False, "error": {"message": "Host stopped before admitting next step"}}
            return await api(action, **kwargs)
        with patch.object(QueryEngine, "run", run):
            with patch.dict(self.engine.tool_context, {"coordinate_agents": interrupted_host}):
                first = await api("pipeline", run_id="restart", **self.spec())
            self.assertEqual(first["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(calls), 2)
            sid = self.engine.session_id
            await self.engine.aclose()
            restored = self.make_engine(); self.assertTrue(restored.load_session(sid))
            restored.permission_manager.approval_policy.set_mode("full")
            after = await restored.agents("pipeline", run_id="restart")
            self.assertEqual(after["data"]["runs"][0]["status"], "completed")
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(p.startswith("SUMMARIZE") for p in calls[2:]))

    async def test_duplicate_ids_reject_before_checkpoint_and_frozen_dispatch_drift_blocks(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        bad = self.spec(); bad["items"][1]["id"] = "fast"
        with patch.object(QueryEngine, "run", completed_run):
            rejected = await self.engine.agents("pipeline", run_id="invalid", **bad)
            self.assertFalse(rejected["ok"])
            self.assertEqual(self.engine.subagent_registry, [])
            self.assertEqual(self.engine.tool_context.get("pipelines", {}), {})
            await self.engine.agents("pipeline", run_id="frozen", **self.spec())
            row = self.engine.subagent_registry[0]
            row["dispatch"]["task"] = "DIFFERENT_INPUT"
            result = await self.engine.agents("pipeline", run_id="frozen")
            self.assertEqual(result["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(self.engine.subagent_registry), 4)

    async def test_join_records_failure_as_finished_without_claiming_success(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        async def fail(child, user_input, sys_print_callback):
            return RunResult("out of budget", "budget_exhausted", True)
        with patch.object(QueryEngine, "run", fail):
            spawn = await self.engine.agents("spawn", prompt="fail")
        identity = spawn["data"]["agents"][0]["agent_id"]
        joined = await self.engine.agents("wait", agent_ids=[identity], wait_for="all", timeout_ms=0)
        self.assertTrue(joined["data"]["condition_met"])
        self.assertFalse(joined["data"]["timed_out"])
        self.assertEqual(joined["data"]["agents"][0]["status"], "crashed")
        self.assertFalse(joined["data"]["agents"][0]["receipt"]["ok"])

    async def test_real_noninteractive_cli_starts_and_resumes_without_duplicate_requests(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                content = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "CLI_STAGE_RESULT"}, "finish_reason": None}]}
                stop = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                self.wfile.write(("data: " + json.dumps(content) + "\n\ndata: " + json.dumps(stop) + "\n\ndata: [DONE]\n\n").encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        config = self.root / "cli-config"; config.mkdir()
        (config / "config.json").write_text(json.dumps({"model_provider": "openai", "active_model_connection": "fixture",
            "model_connections": [{"id": "fixture", "provider": "openai", "model": "fixture-model", "models": ["fixture-model"],
                "api_key": "OWNED_CLI_FIXTURE", "base_url": "http://127.0.0.1:" + str(server.server_port) + "/v1"}]}), encoding="utf-8")
        spec = self.root / "pipeline.json"; spec.write_text(json.dumps(self.spec()), encoding="utf-8")
        env = {**os.environ, "XULOOM_CONFIG_DIR": str(config), "PYTHONPATH": os.environ.get("XULOOM_TEST_SITE") or str(Path(__file__).resolve().parents[1])}
        async def cli(*args):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "pipeline", *args,
                "--cwd", str(self.root), "--json", "--permission", "full", cwd=self.root, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
            self.assertEqual(process.returncode, 0, stderr.decode())
            return json.loads(stdout.decode().splitlines()[-1])["result"]
        try:
            first = await cli("start", "--spec", str(spec), "--run-id", "cli-owned")
            self.assertTrue(first["ok"], first)
            self.assertEqual(len(requests), 4)
            resumed = await cli("resume", "--session", first["session_id"], "--run-id", "cli-owned")
            self.assertTrue(resumed["ok"], resumed)
            self.assertEqual(len(requests), 4)
        finally:
            await asyncio.to_thread(server.shutdown); server.server_close(); thread.join(5)

    async def test_closing_completed_workers_keeps_verified_pipeline_results(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", completed_run):
            result = await self.engine.agents("pipeline", run_id="closed", **self.spec())
            self.assertEqual(result["data"]["runs"][0]["status"], "completed")
            for row in list(self.engine.subagent_registry):
                closed = await self.engine.agents("close", agent_id=row["agent_id"])
                self.assertTrue(closed["ok"], closed)
            again = await self.engine.agents("pipeline", run_id="closed")
            self.assertEqual(again["data"]["runs"][0]["status"], "completed")
            self.assertEqual(len(self.engine.subagent_registry), 4)
            self.engine.subagent_registry[-1]["outcome_unknown"] = True
            uncertain = await self.engine.agents("pipeline", run_id="closed")
            self.assertEqual(uncertain["data"]["runs"][0]["status"], "blocked")
            self.assertEqual(len(self.engine.subagent_registry), 4)
