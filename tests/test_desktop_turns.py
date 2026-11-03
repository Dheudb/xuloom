import unittest
from unittest.mock import patch
from xuloom.desktop.turns import TurnRecorder, conversation_history


class TurnTests(unittest.TestCase):
    def test_compaction_has_one_durable_step_without_duplicate_plain_log(self):
        recorder = TurnRecorder()
        recorder.record["userMessageId"] = "user"
        for state in ("running", "completed"):
            self.assertTrue(recorder.accept({"type":"context_compaction", "id":"owned", "status":state, "before":12000, "after":2000}))
        self.assertFalse(recorder.accept({"type":"log", "text":"上下文已压缩：约 12,000 → 2,000 tokens；原文已归档。"}))
        recorder.accept({"type":"final", "text":"Answer"}); recorder.accept({"type":"turn_finished"})
        self.assertEqual(len(recorder.record["steps"]), 1)
        rows = conversation_history([{"role":"user", "_uuid":"user", "content":"Task"}, {"role":"assistant", "content":"Answer"}], [recorder.record])
        self.assertEqual(rows[-1]["trace"]["steps"][0]["compaction"]["status"], "completed")
        for kind, expected in (("turn_cancelled", "cancelled"), ("turn_error", "interrupted")):
            pending = TurnRecorder(); pending.accept({"type":"context_compaction", "id":"pending", "status":"running"}); pending.accept({"type":kind})
            self.assertEqual(pending.record["steps"][0]["compaction"]["status"], expected)

    def test_resume_keeps_response_identity_partial_text_and_excludes_paused_time(self):
        previous = {"id": "same", "userMessageId": "user", "startedAt": 1000, "status": "cancelled", "elapsedMs": 5000,
            "answer": "First ", "pendingText": "", "steps": [], "files": [], "modelStreams": [{"turn": 1, "status": "cancelled"}]}
        with patch("xuloom.desktop.turns.time.monotonic", side_effect=[100, 102, 102]):
            recorder = TurnRecorder(previous)
            recorder.accept({"type": "turn_started"})
            recorder.accept({"type": "stream_status", "turn": 1, "metrics": {"status": "waiting"}})
            recorder.accept({"type": "message_start"})
            recorder.accept({"type": "text_delta", "text": "continued"})
            self.assertEqual(recorder.record["pendingText"], "First continued")
            recorder.accept({"type": "final", "text": "continued"})
            recorder.accept({"type": "turn_finished"})
        self.assertEqual(recorder.record["answer"], "First continued")
        self.assertEqual(recorder.record["id"], "same")
        self.assertEqual(recorder.record["elapsedMs"], 7000)
        self.assertEqual([row["turn"] for row in recorder.record["modelStreams"]], [1, 2])
        self.assertEqual(previous["status"], "cancelled")
        history = conversation_history([{"role": "user", "_uuid": "user", "content": "task"},
            {"role": "assistant", "content": "First "},
            {"role": "user", "_is_meta": True, "_origin": "resume_control", "content": "internal resume"},
            {"role": "assistant", "content": "continued"}], [recorder.record])
        self.assertEqual([row["text"] for row in history], ["task", "First continued"])

    def test_resume_with_tool_moves_partial_commentary_into_process(self):
        previous = {"id": "same", "status": "cancelled", "elapsedMs": 1000, "answer": "Checking.", "steps": [], "files": []}
        recorder = TurnRecorder(previous)
        recorder.accept({"type": "turn_started"})
        recorder.accept({"type": "message_start"})
        recorder.accept({"type": "tool_started", "tool_use_id": "read", "tool_name": "file_read_tool"})
        recorder.accept({"type": "tool_result", "tool_use_id": "read", "result": "saved evidence"})
        recorder.accept({"type": "message_start"})
        recorder.accept({"type": "text_delta", "text": "Final result"})
        recorder.accept({"type": "final", "text": "Final result"})
        self.assertEqual(recorder.record["answer"], "Final result")
        self.assertEqual(recorder.record["steps"][0]["text"], "Checking.")

    def test_final_keeps_commentary_tools_and_measured_duration(self):
        with patch("xuloom.desktop.turns.time.monotonic", side_effect=[100, 108]):
            r = TurnRecorder()
            r.accept({"type": "text_delta", "text": "先检查文件。"})
            r.accept({"type": "tool_scheduled", "tool_use_id": "one", "tool_name": "file_read_tool"})
            r.accept({"type": "message_stop", "stop_reason": "tool_use"})
            r.accept({"type": "tool_started", "tool_use_id": "one", "tool_name": "file_read_tool", "input": {"file_path": "proof"}})
            r.accept({"type": "tool_result", "tool_use_id": "one", "result": "proof output"})
            r.accept({"type": "message_start"})
            r.accept({"type": "text_delta", "text": "最终答案"})
            r.accept({"type": "final", "text": "最终答案"})
            r.accept({"type": "turn_finished"})
        record = r.record
        self.assertEqual(record["elapsedMs"], 8000)
        self.assertEqual(record["answer"], "最终答案")
        self.assertEqual(record["steps"][0]["text"], "先检查文件。")
        self.assertEqual(len(record["steps"]), 2)
        self.assertEqual(record["steps"][1]["tool"]["result"], "proof output")
        self.assertEqual(record["steps"][1]["tool"]["input"], {"file_path": "proof"})

    def test_cancel_error_and_child_events_do_not_replace_main_process(self):
        for status, event in [("cancelled", "turn_cancelled"), ("error", "turn_error")]:
            r = TurnRecorder()
            r.accept({"type": "text_delta", "text": "未完成的过程"})
            r.accept({"type": "tool_started", "agent_id": "child", "tool_use_id": "child", "tool_name": "bash_tool"})
            r.accept({"type": "tool_started", "tool_use_id": "main", "tool_name": "file_write_tool"})
            r.accept({"type": "permission", "requestId": "approval", "request": {"classifier_input": {"tool_use_id": "main", "agent_role": "main"}}})
            self.assertEqual(r.record["steps"][-1]["tool"]["status"], "waiting")
            r.accept({"type": event})
            r.accept({"type": "turn_finished"})
            self.assertEqual(r.record["status"], status)
            self.assertEqual(len(r.record["steps"]), 2)
            self.assertNotIn("child", str(r.record))
            self.assertEqual(r.record["answer"], "")

    def test_history_groups_old_tool_loops_and_hides_internal_messages(self):
        messages = [
            {"role": "user", "_uuid": "one", "_timestamp": "2026-10-14T01:00:00Z", "content": "检查"},
            {"role": "assistant", "content": [{"type": "text", "text": "先查看"}, {"type": "tool_use", "id": "read", "name": "file_read_tool", "input": {"file_path": "proof"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read", "content": "output"}]},
            {"role": "user", "_is_meta": True, "content": "SYSTEM"},
            {"role": "user", "_origin": "subagent", "content": "CHILD"},
            {"role": "assistant", "_timestamp": "2026-10-14T01:00:08Z", "content": [{"type": "thinking", "thinking": "protected"}, {"type": "text", "text": "完成"}]},
            {"role": "user", "content": "第二轮"}, {"role": "assistant", "content": "你好"},
        ]
        history = conversation_history(messages)
        self.assertEqual([m["text"] for m in history], ["检查", "完成", "第二轮", "你好"])
        trace = history[1]["trace"]
        self.assertEqual(trace["finishedAt"] - trace["startedAt"], 8000)
        self.assertEqual([s["kind"] for s in trace["steps"]], ["text", "tool"])
        self.assertNotIn("protected", str(history))
        self.assertNotIn("trace", history[-1])

    def test_restored_trace_is_authoritative_and_running_becomes_interrupted(self):
        messages = [{"role": "user", "_uuid": "one", "content": "问好"}, {"role": "assistant", "content": "partial"}]
        r = TurnRecorder()
        r.record["userMessageId"] = "one"
        r.accept({"type": "text_delta", "text": "已收到的过程"})
        history = conversation_history(messages, [r.record])
        self.assertEqual(history[-1]["trace"]["status"], "interrupted")
        self.assertEqual(history[-1]["trace"]["steps"][0]["text"], "已收到的过程")
        self.assertEqual(r.record["status"], "running", "Reading must not mutate saved records")
        r.accept({"type": "final", "text": "完成答案"})
        r.accept({"type": "turn_finished"})
        history = conversation_history(messages, [r.record])
        self.assertEqual(history[-1]["text"], "完成答案")
        self.assertEqual(history[-1]["trace"]["elapsedMs"], r.record["elapsedMs"])

    def test_empty_failed_request_still_restores_elapsed_time(self):
        r = TurnRecorder()
        r.record["userMessageId"] = "one"
        r.accept({"type": "turn_error"})
        history = conversation_history([{"role": "user", "_uuid": "one", "content": "hello"}], [r.record])
        self.assertEqual(history[-1]["trace"]["status"], "error")
        self.assertIn("elapsedMs", history[-1]["trace"])
