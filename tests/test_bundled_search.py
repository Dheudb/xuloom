"""Desktop file search must work without a separately installed ripgrep."""
import hashlib
import io
import runpy
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xuloom.tools.grep_tool import GrepTool


class BundledSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_search_uses_bundled_binary_without_path(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "Lib/site-packages/xuloom_search/rg.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
            process = type("Process", (), {"returncode": 0, "communicate": AsyncMock(return_value=(b"proof.txt:1:proof\n", b""))})()
            with patch("sys.prefix", folder), patch.dict("os.environ", {"PATH": ""}), \
                    patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
                result = await GrepTool().execute("proof", path=folder)
            self.assertIn("proof", result)
            self.assertEqual(spawn.await_args.args[0], str(binary))

    async def test_source_install_still_uses_system_ripgrep(self):
        with tempfile.TemporaryDirectory() as folder:
            process = type("Process", (), {"returncode": 1, "communicate": AsyncMock(return_value=(b"", b""))})()
            with patch("sys.prefix", folder), patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
                self.assertEqual(await GrepTool().execute("proof", path=folder), "No matches found.")
            self.assertEqual(spawn.await_args.args[0], "rg")


class SearchPackagingTests(unittest.TestCase):
    def builder(self):
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / "desktop/scripts/bundled_search.py"))

    def archive(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in ("rg.exe", "COPYING", "LICENSE-MIT", "UNLICENSE"):
                archive.writestr("ripgrep-15.2.0-x86_64-pc-windows-msvc/" + name, b"fixture")
        return stream.getvalue()

    def test_verified_archive_copies_binary_and_licenses(self):
        builder = self.builder()
        prepare = builder["prepare_search"]
        raw = self.archive()
        prepare.__globals__["ARCHIVE_SHA256"] = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            cache, target = Path(folder) / "cache", Path(folder) / "target"
            cache.mkdir()
            (cache / builder["ARCHIVE_NAME"]).write_bytes(raw)
            prepare(target, cache=cache)
            self.assertEqual(sorted(p.name for p in (target / "xuloom_search").iterdir()),
                             ["COPYING", "LICENSE-MIT", "UNLICENSE", "rg.exe"])

    def test_corrupt_archive_is_rejected_before_installation(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory() as folder:
            cache, target = Path(folder) / "cache", Path(folder) / "target"
            cache.mkdir()
            (cache / builder["ARCHIVE_NAME"]).write_bytes(b"corrupted archive")
            with self.assertRaisesRegex(ValueError, "checksum"):
                builder["prepare_search"](target, cache=cache)
            self.assertFalse(target.exists())

    def test_resource_update_rejects_an_extra_application_executable(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "AnotherApp.exe").write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "Unexpected executable"):
                builder["validate_search_executables"](root)

    def test_resource_update_only_allows_verified_search_binary(self):
        builder = self.builder()
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "dependencies/xuloom_search/rg.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                builder["validate_search_executables"](Path(folder))
            builder["validate_search_executables"].__globals__["BINARY_SHA256"] = hashlib.sha256(b"tampered").hexdigest()
            builder["validate_search_executables"](Path(folder))


if __name__ == "__main__":
    unittest.main()
