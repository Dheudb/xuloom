import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
import subprocess

from xuloom.maintenance.archive import digest
from xuloom.maintenance.updates import stage_update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.package = self.base / 'Xuloom-win32-x64'
        self.package.mkdir()
        (self.package / 'Xuloom.exe').write_bytes(b'original')
        self.zip = self.base / 'Xuloom-v1.0.0-windows-x64.zip'
        self.files = {'Xuloom.exe': b'next', 'resources/python/python.exe': b'python',
            'resources/app/electron/main.cjs': b'//main', 'resources/app/package.json': b'{"version":"1.0.0"}',
            'RELEASE.json': b'{"version":"1.0.0","tag":"v1.0.0"}'}

    def archive(self):
        with zipfile.ZipFile(self.zip, 'w') as archive:
            for name, content in self.files.items():
                archive.writestr('Xuloom-win32-x64/' + name, content)
        (self.base / 'SHA256SUMS.txt').write_text(digest(self.zip) + '  ' + self.zip.name)

    def test_complete_package_staged_without_modifying_original(self):
        self.archive()
        result = stage_update(self.package, self.zip)
        manifest = json.loads((Path(result['stage']) / 'manifest.json').read_text())
        self.assertEqual(len(manifest['hashes']), len(self.files))
        self.assertEqual((self.package / 'Xuloom.exe').read_bytes(), b'original')

    def test_checksum_mismatch_rejected(self):
        self.archive()
        with self.zip.open('ab') as out:
            out.write(b'corrupt')
        with self.assertRaises(ValueError):
            stage_update(self.package, self.zip)

    def test_traversal_and_incomplete_package_rejected(self):
        self.files['../escaped'] = b'tamper'
        self.archive()
        with self.assertRaises(ValueError):
            stage_update(self.package, self.zip)

        del self.files['../escaped']
        del self.files['resources/python/python.exe']
        self.archive()
        with self.assertRaises(ValueError):
            stage_update(self.package, self.zip)

    @unittest.skipUnless(os.name == 'nt', 'Windows installation exchange')
    def test_windows_swap_and_rollback_keep_original_launch_path(self):
        for name, data in self.files.items():
            target = self.package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.replace(b'1.0.0', b'0.9.0') if name.endswith('.json') else b'original')
        self.archive()
        result = stage_update(self.package, self.zip)
        helper = Path(__file__).resolve().parents[1] / 'desktop/electron/update-package.ps1'
        def apply(result):
            subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(helper),'-Stage',result['stage']],
                           check=True, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(json.loads((Path(result['stage']) / 'status.json').read_text())['state'], 'complete')
        apply(result)
        self.assertEqual((self.package / 'Xuloom.exe').read_bytes(), b'next')
        rollback = stage_update(self.package, rollback=result['rollback'])
        apply(rollback)
        self.assertEqual((self.package / 'Xuloom.exe').read_bytes(), b'original')
