import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from xuloom.features import html_preview as preview


class HtmlPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom-html-rpc-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "nested").mkdir(parents=True)
        self.entry = self.workspace / "nested" / "page.html"
        self.entry.write_text('<link rel="stylesheet" href="../style.css"><h1>remote</h1>', encoding="utf8")

    def test_entry_and_binary_assets_use_bounded_versioned_reads(self):
        asset = self.workspace / "large.wasm"
        content = b"\0asm" + bytes(range(256)) * 3000
        asset.write_bytes(content)
        main = preview.info(self.workspace, str(self.entry), [])
        self.assertEqual(main["asset"], "nested/page.html")
        self.assertEqual(main["mime"], "text/html; charset=utf-8")
        info = preview.info(self.workspace, str(self.entry), [], "large.wasm")
        values = []
        for offset in range(0, len(content), 262144):
            part = preview.read(self.workspace, str(self.entry), [], info["asset"], info["version"], offset)
            self.assertEqual(part["offset"], offset)
            values.append(base64.b64decode(part["data"], validate=True))
        self.assertEqual(b"".join(values), content)
        asset.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "版本"):
            preview.read(self.workspace, str(self.entry), [], info["asset"], info["version"])

    def test_hidden_traversal_unsupported_and_unknown_external_files_are_denied(self):
        (self.workspace / ".secret.json").write_text('"secret"')
        (self.workspace / "script.py").write_text('"secret"')
        outside = self.root / "outside.html"; outside.write_text("external")
        for asset in ("../outside.html", "/outside.html", "nested/../../outside.html", ".secret.json", "script.py", "C:/outside.html", "nested\\page.html", None):
            with self.subTest(asset=asset):
                if asset is None:
                    with self.assertRaises(ValueError): preview.info(self.workspace, str(outside), [])
                else:
                    with self.assertRaises(ValueError): preview.info(self.workspace, str(self.entry), [], asset)

    def test_changed_external_entry_is_previewed_without_neighbors(self):
        outside = self.root / "outside.html"; outside.write_text("external")
        (self.root / "neighbor.json").write_text('"private"')
        known = [{"path": str(outside)}]
        info = preview.info(self.workspace, str(outside), known)
        self.assertEqual(info["asset"], "outside.html")
        part = preview.read(self.workspace, str(outside), known, info["asset"], info["version"])
        self.assertEqual(base64.b64decode(part["data"]), b"external")
        with self.assertRaises(ValueError): preview.info(self.workspace, str(outside), known, "neighbor.json")

    def test_invalid_ranges_and_size_are_denied(self):
        info = preview.info(self.workspace, str(self.entry), [])
        for offset, length in ((-1, 2), (0, 0), (0, 524289), (True, 2), (0, True), (1000000, 2)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                preview.read(self.workspace, str(self.entry), [], info["asset"], info["version"], offset, length)
        large = self.workspace / "huge.txt"
        with large.open("wb") as stream: stream.truncate(preview.LIMIT + 1)
        with self.assertRaises(ValueError): preview.info(self.workspace, str(self.entry), [], "huge.txt")

    def test_symlink_escape_is_denied(self):
        target = self.root / "outside"; target.mkdir(); (target / "secret.css").write_text("private")
        link = self.workspace / "linked"
        try: link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt": self.skipTest("Symlink creation is unavailable on this host")
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "New-Item -ItemType Junction -Path $env:XULOOM_FIXTURE_LINK -Target $env:XULOOM_FIXTURE_TARGET | Out-Null"],
                env={**os.environ, "XULOOM_FIXTURE_LINK": str(link), "XULOOM_FIXTURE_TARGET": str(target)},
                capture_output=True, timeout=15, creationflags=0x08000000)
            if result.returncode: self.skipTest("Neither symlinks nor directory junctions are available")
        with self.assertRaises(ValueError): preview.info(self.workspace, str(self.entry), [], "linked/secret.css")


if __name__ == "__main__": unittest.main()
