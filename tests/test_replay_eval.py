import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
from xuloom.models import ToolCall, model_response
from xuloom.models.registry import get_adapter
from xuloom.testing import EvalCase, ReplayModel, ScriptedModel, load_cases, load_cassette, run_eval
from xuloom.utils.vcr import ReplayMissError, VCRManager

ROOT = Path(__file__).resolve().parents[1]


class Isolated(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.root / "home"),
                                           "USERPROFILE": str(self.root / "home")})
        self.env.start()
        self.base = EngineConfig(cwd=str(self.root / "work"), approval_policy=ApprovalPolicy("full"),
                                 collaboration_mode="solo")

    async def asyncTearDown(self):
        self.env.stop()
        self.temp.cleanup()


class ScriptedModelTests(Isolated):
    async def test_script_drives_tools_and_records_requests(self):
        model = ScriptedModel([
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "hi"})]),
            model_response(text="written"),
        ])
        with model:
            self.assertIs(get_adapter(model.name), model)
            async with QueryEngine(model.config(self.base)) as engine:
                result = await engine.run("write", sys_print_callback=lambda *_: None)
        self.assertIsNone(get_adapter(model.name))
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "written")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual((self.root / "work" / "a.txt").read_text(), "hi")

    async def test_exhausted_script_fails_the_run_instead_of_hanging(self):
        with ScriptedModel([]) as model:
            async with QueryEngine(model.config(self.base)) as engine:
                result = await engine.run("hi", sys_print_callback=lambda *_: None)
        self.assertFalse(result.ok)
        self.assertIn("ScriptExhausted", result.text + result.reason + str(engine.last_run_result))


class RecordReplayTests(Isolated):
    async def test_recorded_run_replays_offline_with_identical_outcome(self):
        cassette = self.root / "run.jsonl"
        live = ScriptedModel([
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "out.txt", "content": "v1"})],
                           input_tokens=10, output_tokens=3),
            model_response(text="done", input_tokens=12, output_tokens=1),
        ])
        with live:
            async with QueryEngine(live.config(self.base, record_responses=str(cassette))) as engine:
                first = await engine.run("write out.txt", sys_print_callback=lambda *_: None)
        self.assertTrue(first.ok)
        records = load_cassette(cassette)
        self.assertEqual([row["turn"] for row in records], [1, 2])
        self.assertEqual(records[0]["response"]["content"][0]["name"], "file_write_tool")
        (self.root / "work" / "out.txt").unlink()

        with ReplayModel(cassette) as replay:
            async with QueryEngine(replay.config(self.base)) as engine:
                second = await engine.run("write out.txt", sys_print_callback=lambda *_: None)
        self.assertTrue(second.ok, second)
        self.assertEqual(second.text, "done")
        self.assertEqual((self.root / "work" / "out.txt").read_text(), "v1")
        self.assertEqual(engine.session_token_usage["input_tokens"], 22)

    async def test_environment_records_only_the_top_level_engine(self):
        cassette = self.root / "env.jsonl"
        with ScriptedModel([model_response(text="ok")]) as model, \
                patch.dict(os.environ, {"XULOOM_RECORD_RESPONSES": str(cassette)}):
            async with QueryEngine(model.config(self.base)) as engine:
                await engine.run("hi", sys_print_callback=lambda *_: None)
            child = QueryEngine(model.config(self.base, agent_depth=1))
            self.assertIsNone(child.response_recorder)
            await child.aclose()
        self.assertEqual(len(load_cassette(cassette)), 1)

    def test_bad_cassettes_are_reported(self):
        path = self.root / "bad.jsonl"
        path.write_text('{"xuloom_cassette": 1}\n{"turn": 1}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing 'response'"):
            load_cassette(path)
        path.write_text("not json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a JSON line"):
            ReplayModel(path)


class StrictVcrTests(unittest.TestCase):
    def test_strict_replay_never_falls_back_to_a_live_request(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = VCRManager(cassette_dir=folder, mode="replay")
            payload = manager.build_request_payload(model="m", system="", messages=[], tools=[], max_tokens=10)
            with self.assertRaises(ReplayMissError):
                manager.try_replay(payload)
            Path(manager._cassette_path(manager._hash_request(payload))).write_text("{", encoding="utf-8")
            with self.assertRaises(ReplayMissError):
                manager.try_replay(payload)
            self.assertIsNone(VCRManager(cassette_dir=folder, mode="off").try_replay(payload))


class EvalTests(Isolated):
    async def test_cases_pass_and_fail_on_their_checks(self):
        model = ScriptedModel([
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "hello.txt", "content": "hi there"})]),
            model_response(text="Created hello.txt"),
            model_response(text="I refuse"),
        ])
        cases = [
            EvalCase("writes", "create hello.txt", checks=[
                {"type": "ok"}, {"type": "file_contains", "path": "hello.txt", "text": "hi"},
                {"type": "tool_called", "name": "file_write_tool"}, {"type": "contains", "text": "created", "ignore_case": True},
                {"type": "max_requests", "value": 2}]),
            EvalCase("refuses", "create hello.txt", files={"seed.txt": "x"}, checks=[
                {"type": "file_exists", "path": "hello.txt"}, {"type": "file_exists", "path": "seed.txt"},
                {"type": "regex", "pattern": "^I refuse$"}]),
        ]
        with model:
            report = await run_eval(cases, model.config(self.base))
        first, second = report.cases
        self.assertTrue(first.passed, first)
        self.assertEqual(first.tools, ["file_write_tool"])
        self.assertEqual(first.requests, 2)
        self.assertFalse(second.passed)
        self.assertEqual([check["passed"] for check in second.checks], [False, True, True])
        self.assertEqual(report.to_dict()["passed"], 1)
        self.assertFalse(report.ok)

    async def test_case_paths_cannot_leave_the_workspace(self):
        with ScriptedModel([model_response(text="ok")]) as model:
            report = await run_eval([EvalCase("escape", "hi", checks=[{"type": "file_exists", "path": "../../etc/passwd"}])],
                                    model.config(self.base))
        check, = report.cases[0].checks
        self.assertFalse(check["passed"])
        self.assertIn("leaves the case workspace", check["detail"])

    def test_case_files_are_validated(self):
        path = self.root / "cases.json"
        path.write_text(json.dumps([{"id": "a", "prompt": "x", "checks": [{"type": "nope"}]}]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown check"):
            load_cases(path)
        path.write_text(json.dumps({"cases": [{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_cases(path)
        yaml_path = self.root / "cases.yaml"
        yaml_path.write_text("- id: a\n  prompt: x\n  cassette: run.jsonl\n", encoding="utf-8")
        case, = load_cases(yaml_path)
        self.assertEqual(case.cassette, str((self.root / "run.jsonl").resolve()))


class EvalCliTests(unittest.TestCase):
    def test_cli_replays_cases_offline_and_sets_the_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cassette = root / "write.jsonl"
            rows = [{"xuloom_cassette": 1},
                    {"turn": 1, "response": {"stop_reason": "tool_use", "content": [
                        {"type": "tool_use", "id": "c1", "name": "file_write_tool", "input": {"file_path": "x.txt", "content": "ok"}}]}},
                    {"turn": 2, "response": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}]
            cassette.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            cases = root / "cases.json"
            cases.write_text(json.dumps([
                {"id": "passes", "prompt": "write x", "cassette": "write.jsonl",
                 "checks": [{"type": "ok"}, {"type": "file_contains", "path": "x.txt", "text": "ok"}]},
                {"id": "fails", "prompt": "write x", "cassette": "write.jsonl",
                 "checks": [{"type": "contains", "text": "something else"}]}]), encoding="utf-8")
            env = {**os.environ, "XULOOM_DATA_DIR": str(root / "data"), "HOME": str(root / "home"),
                   "USERPROFILE": str(root / "home"), "PYTHONPATH": str(ROOT)}
            for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XULOOM_MODEL_PROVIDER"):
                env.pop(key, None)
            run = subprocess.run([sys.executable, "-m", "xuloom", "eval", str(cases), "--json", "--permission", "full"],
                                 capture_output=True, text=True, encoding="utf-8", env=env, cwd=folder, timeout=300)
            final = json.loads(run.stdout.strip().splitlines()[-1])["result"]
            self.assertEqual(run.returncode, 10, run.stderr)
            self.assertEqual((final["passed"], final["total"]), (1, 2))
            only = subprocess.run([sys.executable, "-m", "xuloom", "eval", str(cases), "--only", "passes", "--permission", "full"],
                                  capture_output=True, text=True, encoding="utf-8", env=env, cwd=folder, timeout=300)
            self.assertEqual(only.returncode, 0, only.stdout + only.stderr)
            self.assertIn("PASS  passes", only.stdout)


if __name__ == "__main__":
    unittest.main()
