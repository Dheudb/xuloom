import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows resource updater")
class UpdateMetadataTests(unittest.TestCase):
    def test_upgrade_retires_old_metadata_and_rollback_restores_it(self):
        script = Path(__file__).resolve().parents[1] / "desktop/scripts/update-existing.ps1"
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); package = root / "package"; stage = root / "stage"
                site = package / "resources/python/Lib/site-packages"
                old = site / "xuloom-0.1.0.dist-info"
                old.mkdir(parents=True); (old / "METADATA").write_text("old")
                (package / "Xuloom.exe").write_bytes(b"test fixture, not executable")
                for folder in ("app", "xuloom", "metadata"):
                    (stage / folder).mkdir(parents=True)
                    (stage / folder / "fixture").write_text("new")
                (package / "resources/app").mkdir()
                lock = package / "resources/app/locked.txt"; lock.write_text("old")
                (stage / "manifest.json").write_text(json.dumps({"metadataDirectory": "xuloom-0.1.1.dist-info", "pythonPackages": [],
                    "originalExeSha256":hashlib.sha256((package/"Xuloom.exe").read_bytes()).hexdigest()}))
                handle = lock.open("rb") if fail else None
                try:
                    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                        "-PackageDirectory", str(package), "-StagedResources", str(stage), "-WaitSeconds", "0"], capture_output=True, timeout=45)
                finally:
                    if handle: handle.close()
                self.assertEqual(result.returncode, 1 if fail else 0, result.stderr.decode(errors="replace"))
                self.assertEqual(old.exists(), fail)
                self.assertEqual((site / "xuloom-0.1.1.dist-info").exists(), not fail)
                if fail: self.assertEqual((old / "METADATA").read_text(), "old")
