"""Follow-up delivery through the real scheduler, worker journal and session checkpoint."""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_agent_lifecycle as lifecycle
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult, PersistenceError
from xuloom.features import schedules


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.LifecycleTests.asyncSetUp
    asyncTearDown = lifecycle.LifecycleTests.asyncTearDown
    make_engine = lifecycle.LifecycleTests.make_engine

    def watch(self, **changes):
        options = dict(due_at=time.time()-1, trigger={"kind": "file", "path": "experiment.done"}, request_id="experiment-1")
        options.update(changes)
        return schedules.add(self.engine, "Inspect the saved results", **options)

    def due(self, identity):
        with schedules.store() as rows: rows[identity]["due_at"] = time.time()-1

    async def test_waiting_is_quiet_duplicate_events_deliver_once_and_lost_ack_survives_marker_removal(self):
        row = self.watch()
        self.assertEqual(self.watch()["id"], row["id"])
        execute = AsyncMock(return_value={"ok": True, "persisted": True, "reason": "completed"})
        self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "waiting")
        execute.assert_not_awaited()
        marker = self.root / "experiment.done"
        marker.write_text("ready")
        self.due(row["id"])
        before = copy.deepcopy(schedules.list_items()[0])
        receipt = await schedules.deliver(self.engine, row["id"], execute)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["acceptance"], "execution_only")
        self.assertEqual(receipt["input_status"], "processed")
        self.assertIn("marker_observed_not_task_verified", execute.call_args.args[0])
        with schedules.store() as rows: rows[row["id"]] = before
        marker.unlink()
        await schedules.deliver(self.engine, row["id"], execute)
        execute.assert_awaited_once()
        self.assertEqual(self.watch()["status"], "completed")

    async def test_scope_changes_and_id_reuse_cannot_retarget_authorized_work(self):
        row = self.watch()
        with self.assertRaisesRegex(ValueError, "different follow-up"):
            schedules.add(self.engine, "Different work", due_at=time.time(), trigger={"kind": "file", "path": "experiment.done"}, request_id="experiment-1")
        (self.root / "experiment.done").write_text("ready")
        self.engine.primary_model = "different-model"
        execute = AsyncMock()
        result = await schedules.deliver(self.engine, row["id"], execute)
        self.assertEqual(result["status"], "blocked")
        execute.assert_not_awaited()

    async def test_cancel_busy_and_interrupted_delivery_do_not_replay(self):
        row = self.watch()
        (self.root / "experiment.done").write_text("ready")
        self.engine.active_run_task = asyncio.current_task()
        execute = AsyncMock(side_effect=asyncio.CancelledError())
        try: self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "queued")
        finally: self.engine.active_run_task = None
        schedules.change(row["id"], "pause")
        self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "not_due")
        execute.assert_not_awaited()
        schedules.change(row["id"], "resume")
        with self.assertRaises(asyncio.CancelledError): await schedules.deliver(self.engine, row["id"], execute)
        (self.root / "experiment.done").unlink()
        retry = AsyncMock()
        self.assertEqual((await schedules.deliver(self.engine, row["id"], retry))["status"], "blocked")
        retry.assert_not_awaited()

    async def test_failed_final_save_does_not_acknowledge_or_retry(self):
        row = self.watch()
        (self.root / "experiment.done").write_text("ready")
        real_save = self.engine.persist_session_state
        def save():
            return False if self.engine.scheduled_deliveries.get(row["id"], {}).get("status") == "completed" else real_save()
        execute = AsyncMock(return_value={"ok": True, "persisted": True})
        with patch.object(self.engine, "persist_session_state", side_effect=save):
            with self.assertRaises(PersistenceError): await schedules.deliver(self.engine, row["id"], execute)
            with self.assertRaises(PersistenceError): await schedules.deliver(self.engine, row["id"], execute)
        execute.assert_awaited_once()

    async def test_restart_reads_receipt_instead_of_reexecuting_effect(self):
        row = self.watch()
        (self.root / "experiment.done").write_text("ready")
        before = copy.deepcopy(schedules.list_items()[0])
        await schedules.deliver(self.engine, row["id"], AsyncMock(return_value={"ok": True, "persisted": True}))
        self.engine.session_manager.release_writer()
        restored = self.make_engine()
        self.assertTrue(restored.load_session(self.engine.session_id))
        with schedules.store() as rows: rows[row["id"]] = before
        retry = AsyncMock()
        self.assertEqual((await schedules.deliver(restored, row["id"], retry))["status"], "completed")
        retry.assert_not_awaited()

    async def test_all_workers_wait_then_failed_worker_is_evidence_not_success(self):
        gates = [asyncio.Event(), asyncio.Event()]
        started = []
        async def run(child, user_input, sys_print_callback):
            index = len(started); started.append(child.agent_id)
            await gates[index].wait()
            return RunResult("saved result", "completed" if index == 0 else "api_error", True)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            await self.tool.execute(prompt="Investigate", count=2, run_in_background=True, response_format="json")
            for _ in range(100):
                if len(started) == 2: break
                await asyncio.sleep(0.01)
            self.assertEqual(len(started), 2)
            row = self.watch(trigger={"kind": "workers", "agent_ids": started})
            execute = AsyncMock(return_value={"ok": True, "persisted": True})
            gates[0].set()
            await self.engine.tool_context["subagent_tasks"].tasks[started[0]]
            self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "waiting")
            execute.assert_not_awaited()
            gates[1].set()
            await self.engine.tool_context["subagent_tasks"].tasks[started[1]]
            self.due(row["id"])
            receipt = await schedules.deliver(self.engine, row["id"], execute)
            self.assertFalse(receipt["evidence"]["all_succeeded"])
            self.assertEqual(len(receipt["evidence"]["workers"]), 2)
            self.assertEqual(receipt["evidence"]["workers"][1]["verified_success"], False)
            execute.assert_awaited_once()

    async def test_outside_and_oversized_markers_and_unknown_workers_fail_closed(self):
        with self.assertRaises(ValueError): self.watch(trigger={"kind": "file", "path": "../outside"})
        with self.assertRaises(ValueError): self.watch(trigger={"kind": "workers", "agent_ids": ["missing"]})
        row = self.watch()
        (self.root / "experiment.done").write_bytes(b"x" * 65537)
        execute = AsyncMock()
        self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "blocked")
        execute.assert_not_awaited()

    async def test_user_stop_remains_a_pause_after_restart_until_explicit_resume(self):
        row = self.watch()
        self.engine.last_run_result = RunResult("Stopped", "cancelled", True, user_message_id="owned-turn").to_dict()
        self.assertTrue(self.engine.persist_session_state())
        self.engine.session_manager.release_writer()
        restored = self.make_engine()
        self.assertTrue(restored.load_session(self.engine.session_id))
        (self.root / "experiment.done").write_text("ready")
        execute = AsyncMock(return_value={"ok": True, "persisted": True})
        self.assertEqual((await schedules.deliver(restored, row["id"], execute))["status"], "paused")
        execute.assert_not_awaited()
        self.assertEqual(schedules.list_items()[0]["status"], "paused")
        schedules.change(row["id"], "resume")
        self.assertEqual((await schedules.deliver(restored, row["id"], execute))["status"], "completed")
        execute.assert_awaited_once()

    async def test_missing_worker_result_and_unpersisted_followup_do_not_claim_success(self):
        async def run(child, user_input, sys_print_callback):
            return RunResult("Saved result", "completed", True)
        self.engine.permission_manager.approval_policy.set_mode("full")
        with patch.object(QueryEngine, "run", run):
            await self.tool.execute(prompt="Investigate", run_in_background=False, response_format="json")
        record = self.engine.subagent_registry[-1]
        row = self.watch(trigger={"kind": "workers", "agent_ids": [record["agent_id"]]})
        record["result"] = "Unconfirmed replacement"
        execute = AsyncMock()
        self.assertEqual((await schedules.deliver(self.engine, row["id"], execute))["status"], "blocked")
        execute.assert_not_awaited()
        marker = self.watch(request_id="different-event")
        (self.root / "experiment.done").write_text("ready")
        receipt = await schedules.deliver(self.engine, marker["id"], AsyncMock(return_value={"ok": True, "persisted": False}))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["input_status"], "unconfirmed")
        self.assertEqual(next(r for r in schedules.list_items() if r["id"] == marker["id"])["status"], "blocked")
