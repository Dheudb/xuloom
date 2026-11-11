import base64
import json
import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
from xuloom.core.storage import history_root
from xuloom.core.engine import QueryEngine
from xuloom.desktop.attachments import prepare_attachments
from xuloom.desktop.turns import conversation_history


class AttachmentTests(unittest.TestCase):
    def test_visual_bytes_and_file_references_are_separate_from_displayed_text(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XULOOM_DATA_DIR": directory}):
            root = history_root(directory) / "tool-results/session/attachments"; root.mkdir(parents=True)
            image_id, pdf_id = str(uuid.uuid4()), str(uuid.uuid4())
            Image.new("RGBA", (32, 20), (0, 0, 0, 0)).save(root / (image_id + ".png"))
            (root / (pdf_id + ".pdf")).write_bytes(b"%PDF-local-source")
            for identity, kind, ext in [(image_id, "image", ".png"), (pdf_id, "file", ".pdf")]:
                (root / (identity + ".json")).write_text(json.dumps({"id": identity, "file": identity + ext, "name": "附件" + ext, "kind": kind}), encoding="utf-8")
            blocks, items = prepare_attachments(directory, "session", [image_id, pdf_id])
            references = [json.loads(line) for line in blocks[0]["text"].splitlines() if line.startswith("{")]
            self.assertEqual(references[1]["path"], (root / (pdf_id + ".pdf")).as_posix())
            self.assertEqual(blocks[1]["type"], "image")
            self.assertTrue(base64.b64decode(blocks[1]["source"]["data"]).startswith(b"\xff\xd8"))
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(blocks[1]["source"]["data"]))).getpixel((15, 10)), (255, 255, 255))
            engine = QueryEngine.__new__(QueryEngine)
            messages = [{"role": "user", "_uuid": "user", "_display_text": "解释附件", "_attachments": items,
                         "content": [{"type": "text", "text": "解释附件"}] + blocks}]
            rendered = engine._clean_roles_for_openai_api(messages, "system")
            self.assertTrue(any(part["type"] == "image_url" for part in rendered[-1]["content"]))
            history = conversation_history(messages)
            self.assertEqual(history[0]["text"], "解释附件")
            self.assertEqual(len(history[0]["attachments"]), 2)
            with self.assertRaises(ValueError): prepare_attachments(directory, "session", ["../../invalid"])
            raw = json.loads((root / (pdf_id + ".json")).read_text()); raw["file"] = "../outside.pdf"
            (root / (pdf_id + ".json")).write_text(json.dumps(raw))
            with self.assertRaises(ValueError): prepare_attachments(directory, "session", [pdf_id])

    def test_image_tool_results_keep_pixels_and_follow_all_tool_results(self):
        engine = QueryEngine.__new__(QueryEngine)
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        messages = [{"role": "assistant", "content": [{"type": "tool_use", "id": id, "name": "file_read_tool", "input": {}} for id in ["one", "two"]]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "one", "content": [{"type": "text", "text": "image"}, image]},
                                                 {"type": "tool_result", "tool_use_id": "two", "content": "second"}]}]
        rendered = engine._clean_roles_for_openai_api(messages, "")
        self.assertEqual([message["role"] for message in rendered], ["assistant", "tool", "tool", "user"])
        self.assertEqual(rendered[-1]["content"][0]["image_url"]["url"], "data:image/png;base64,AA==")
        native = engine._clean_roles_for_api(messages)
        self.assertEqual(native[-1]["content"][0]["content"][1], image)
