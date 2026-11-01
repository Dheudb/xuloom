"""Recovery proofs must survive Unicode, append-only history and storage faults."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from xuloom.agents.external.pi_history import verify_file, current_messages, verify_receipts
from xuloom.agents.external.runtime import ExternalExecutionError, tool_result_digest, restore
from xuloom.core.run_result import PersistenceError


class PiHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom-pi-history-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "session.jsonl"
        self.state = {"session_id": "owned-session"}
        self.header = {"type": "session", "version": 3, "id": "owned-session"}
        self.first = {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "A\u2028B\u2029C"}}
        self.path.write_bytes(self.line(self.header) + self.line(self.first))

    def line(self, value):
        return (json.dumps(value, ensure_ascii=False) + "\n").encode()

    def test_unicode_separators_stay_inside_a_jsonl_record(self):
        proof = verify_file(self.path, self.state, {"entries": [self.first], "leafId": "a"})
        self.assertEqual(proof["history_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(proof["history_offset"], self.path.stat().st_size)

    def test_incremental_export_validates_only_new_records_and_the_unchanged_prefix(self):
        old = verify_file(self.path, self.state, {"entries": [self.first], "leafId": "a"})
        new = {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "content": [], "stopReason": "aborted"}}
        with self.path.open("ab") as stream: stream.write(self.line(new))
        proof = verify_file(self.path, {**self.state, **old}, {"entries": [new], "leafId": "b"})
        self.assertEqual(proof["entry_cursor"], "b")
        self.assertEqual(proof["history_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.path.write_bytes(self.path.read_bytes().replace(b'A', b'X', 1))
        with self.assertRaises(PersistenceError):
            verify_file(self.path, {**self.state, **old}, {"entries": [new], "leafId": "b"})

    def test_missing_tail_wrong_leaf_and_failed_fsync_never_prove_a_checkpoint(self):
        with self.assertRaises(PersistenceError): verify_file(self.path, self.state, {"entries": [self.first, {"id": "b"}], "leafId": "b"})
        with self.assertRaises(ExternalExecutionError): verify_file(self.path, self.state, {"entries": [self.first], "leafId": "other"})
        with patch("os.fsync", side_effect=OSError("fixture disk failure")), self.assertRaises(PersistenceError):
            verify_file(self.path, self.state, {"entries": [self.first], "leafId": "a"})
        self.assertEqual(set(self.state), {"session_id"})

    def tool_proof(self):
        content = [{"type": "text", "text": "COMPLETE_RESULT"}]
        host = SimpleNamespace(calls={"call": ("file_write_tool", {"file_path": "output.txt", "content": "once"})},
            started_calls={"call"}, receipts={"call": ("file_write_tool", tool_result_digest(content, False))},
            engine=SimpleNamespace(in_progress_tools={}))
        messages = [{"role": "assistant", "content": [{"type": "toolCall", "id": "call", "name": "mcp__xuloom__file_write_tool",
            "arguments": {"file_path": "output.txt", "content": "once", "_xuloom_source_id": "call"}}]},
            {"role": "toolResult", "toolCallId": "call", "toolName": "mcp__xuloom__file_write_tool", "content": content, "isError": False}]
        return host, messages

    def test_matching_id_alone_does_not_prove_the_native_result_was_received(self):
        host, messages = self.tool_proof()
        verify_receipts(host, messages)
        messages[-1]["content"] = [{"type": "text", "text": "TRUNCATED"}]
        with self.assertRaises(ExternalExecutionError): verify_receipts(host, messages)

    def test_missing_native_receipt_or_live_effect_blocks_recovery(self):
        host, messages = self.tool_proof()
        host.receipts.clear()
        with self.assertRaises(ExternalExecutionError): verify_receipts(host, messages)
        host, messages = self.tool_proof()
        host.engine.in_progress_tools["external-call"] = {"phase": "executing"}
        with self.assertRaises(ExternalExecutionError): verify_receipts(host, messages)

    def test_unexecuted_tool_must_have_an_explicit_error_receipt(self):
        host, messages = self.tool_proof()
        host.started_calls.clear(); host.receipts.clear()
        with self.assertRaises(ExternalExecutionError): verify_receipts(host, messages)
        messages[-1]["isError"] = True
        verify_receipts(host, messages)

    def test_starting_cursor_cannot_silently_fall_back_to_old_messages(self):
        entries = [{"id": "old", "type": "message", "message": {"role": "user", "content": "old"}}, self.first]
        self.assertEqual(current_messages({"entries": entries}, "old", None), [self.first["message"]])
        with self.assertRaises(ExternalExecutionError): current_messages({"entries": entries}, "missing", None)

    def test_partial_fingerprint_checkpoint_cannot_replace_engine_state(self):
        state = {"backend": "pi", "phase": "idle", "session_id": "8d0262de-e7f8-4b4c-8025-b676ad07e880", "seen_tool_ids": [], "message_cursor": 0,
            "history_sha256": "a" * 64, "history_offset": 10}
        engine = SimpleNamespace(tool_context={})
        with self.assertRaises(ValueError): restore(engine, {"external_agent": state})
        self.assertEqual(engine.tool_context, {})


if __name__ == "__main__": unittest.main()
