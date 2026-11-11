"""Replay of runs whose recordings include context compaction (and other auxiliary requests)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
from xuloom.models import model_response
from xuloom.testing import ReplayModel, ScriptedModel, ScriptExhausted, load_cassette

QUIET = dict(sys_print_callback=lambda *_: None)


class Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.root / "home"),
                                           "USERPROFILE": str(self.root / "home")})
        self.env.start()
        os.environ.pop("XULOOM_VCR_MODE", None)
        self.base = EngineConfig(model="m", cwd=str(self.root / "work"), collaboration_mode="solo",
                                 approval_policy=ApprovalPolicy("full"))

    async def asyncTearDown(self):
        self.env.stop()
        self.temp.cleanup()

    async def scenario(self, engine):
        """Several long turns, a compaction, then one more question."""
        answers = []
        for index in range(6):
            answers.append((await engine.run(f"question {index} " + "q" * 3000, **QUIET)).text)
        await engine._apply_layered_compaction_if_needed(lambda *_: None, force=True)
        answers.append((await engine.run("final question", **QUIET)).text)
        return answers

    def live_script(self):
        script = [model_response(text=f"answer {index} " + "a" * 3000, input_tokens=10, output_tokens=10) for index in range(6)]
        script.append(model_response(text="SUMMARY: six questions were answered.", input_tokens=50, output_tokens=8))
        script.append(model_response(text="final answer", input_tokens=10, output_tokens=2))
        return script


class SequenceReplayTests(Fixture):
    async def record(self, cassette):
        live = ScriptedModel(self.live_script())
        with live:
            async with QueryEngine(live.config(self.base, record_responses=str(cassette))) as engine:
                answers = await self.scenario(engine)
        self.assertEqual(live.script, [])  # the compaction really asked the model
        return answers

    async def test_compaction_is_recorded_and_replayed_in_place(self):
        cassette = self.root / "run.jsonl"
        recorded = await self.record(cassette)
        rows = load_cassette(cassette)
        self.assertEqual([row.get("purpose") for row in rows if "purpose" in row], ["compaction"])
        with ReplayModel(cassette) as replay:
            async with QueryEngine(replay.config(self.base)) as engine:
                replayed = await self.scenario(engine)
                summary = json.dumps(engine.messages, ensure_ascii=False)
        self.assertEqual(replayed, recorded)
        self.assertEqual(replayed[-1], "final answer")
        self.assertIn("SUMMARY: six questions were answered.", summary)

    async def test_unrecorded_auxiliary_request_fails_instead_of_consuming_a_turn(self):
        cassette = self.root / "turns-only.jsonl"
        rows = [{"xuloom_cassette": 1}] + [
            {"turn": 1, "response": {"stop_reason": "end_turn", "content": [{"type": "text", "text": f"answer {i}"}]}}
            for i in range(2)]
        cassette.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        with ReplayModel(cassette) as replay:
            async with QueryEngine(replay.config(self.base)) as engine:
                first = await engine.run("q " + "q" * 3000, **QUIET)
                with self.assertRaises(Exception) as caught:
                    await engine._apply_layered_compaction_if_needed(lambda *_: None, force=True)
                second = await engine.run("next", **QUIET)
        chain, error = [], caught.exception
        while error is not None:
            chain.append(type(error).__name__)
            error = error.__cause__ or error.__context__
        self.assertIn(ScriptExhausted.__name__, chain)
        self.assertEqual((first.text, second.text), ("answer 0", "answer 1"))  # turns stay aligned

    def test_direct_completion_without_a_purpose_is_refused(self):
        import asyncio
        replay = ReplayModel([{"response": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "turn"}]}}])
        with self.assertRaises(ScriptExhausted):
            asyncio.run(replay.complete_text(None, model="m", messages=[{"role": "user", "content": "x"}], max_tokens=10))
        self.assertEqual(len(replay.script), 1)


class HashReplayTests(Fixture):
    """Legacy request-hash VCR: auxiliary requests are recorded and answered by their own payload."""

    async def call(self, engine, model, text):
        from xuloom.core.config import safe_llm_call
        return await safe_llm_call(engine.compactor.client, "m", model.name, [{"role": "user", "content": text}], 100,
                                   usage_callback=engine.compactor.usage_callback, purpose="compaction",
                                   guard=engine.compactor.request_guard)

    async def test_recorded_auxiliary_request_is_replayed_without_the_model(self):
        cassettes = str(self.root / "vcr")
        live = ScriptedModel([model_response(text="SUMMARY", input_tokens=5, output_tokens=2)])
        with live:
            async with QueryEngine(live.config(self.base, model_prices={"m": {"input": 1, "output": 1}})) as engine:
                engine.vcr_manager.cassette_dir, engine.vcr_manager.mode = cassettes, "record"
                self.assertEqual(await self.call(engine, live, "summarize this history"), "SUMMARY")
        offline = ScriptedModel([])
        with offline:
            async with QueryEngine(offline.config(self.base, model_prices={"m": {"input": 1, "output": 1}})) as engine:
                engine.vcr_manager.cassette_dir, engine.vcr_manager.mode = cassettes, "replay"
                self.assertEqual(await self.call(engine, offline, "summarize this history"), "SUMMARY")
                self.assertEqual(engine.cost_meter.task_usd, 0.0)  # replayed: nothing spent
                from xuloom.utils.vcr import ReplayMissError
                with self.assertRaises(ReplayMissError):
                    await self.call(engine, offline, "a different history")
        self.assertEqual(offline.requests, [])


class LegacyVcrNoticeTests(unittest.TestCase):
    def test_legacy_mode_warns_once_and_off_stays_silent(self):
        from xuloom.utils import vcr
        vcr._legacy_warned = False
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XULOOM_VCR_MODE", None)
            with self.assertNoLogs("xuloom.vcr", "WARNING"):
                vcr.VCRManager(cassette_dir="unused")
                vcr.VCRManager(cassette_dir="unused", mode="off")
        with self.assertLogs("xuloom.vcr", "WARNING") as logs:
            vcr.VCRManager(cassette_dir="unused", mode="replay")
            vcr.VCRManager(cassette_dir="unused", mode="record")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ReplayModel", logs.output[0])


if __name__ == "__main__":
    unittest.main()
