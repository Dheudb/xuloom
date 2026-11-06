"""Packaging-only omissions retain runtime code, native libraries and legal notices."""
import hashlib
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from tests import test_distribution_lifecycle as distribution

ROOT = Path(__file__).resolve().parents[1]
policy = runpy.run_path(str(ROOT / 'desktop/scripts/package_content.py'))
release = runpy.run_path(str(ROOT / 'desktop/scripts/release.py'))
SITE = 'resources/python/Lib/site-packages/'


class PackageContentTests(unittest.TestCase):
    def test_only_explicit_development_paths_are_omitted(self):
        omitted = policy['omitted_reason']
        for name in ['pymupdf/mupdf-devel/include/mupdf/classes.h', 'win32com/test/test.py',
                     'jsonschema/tests/test_validators.py', 'PyWin32.chm', 'openai/__pycache__/client.pyc']:
            self.assertIsNotNone(omitted(SITE + name), name)
        for name in ['pymupdf/_mupdf.pyd', 'pymupdf/mupdfcpp64.dll', 'PIL/_avif.pyd', 'primp/primp.pyd',
                     'pymupdf/mupdf-devel/COPYING', 'win32com/test/NOTICE.txt', 'openai/types/tests.py',
                     'anthropic/resources/workspaces/tests.py', 'certifi/cacert.pem', 'mcp/client/stdio/__init__.py',
                     'win32com/client/__init__.py', 'pythonwin/win32ui.pyd', 'openai/py.typed']:
            self.assertIsNone(omitted(SITE + name), name)
        for name in ['Xuloom.exe', 'LICENSES.chromium.html', 'locales/ja.pak', 'locales/zh-CN.pak', 'resources/python/python312.dll']:
            self.assertIsNone(omitted(name), name)
        for name in ['../outside', '/absolute', 'resources\\python\\bad']:
            with self.assertRaises(ValueError): omitted(name)

    def test_retirement_plan_is_read_only_hashed_and_excludes_replacements(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            files={SITE+'pymupdf/mupdf-devel/proof.h':b'header', SITE+'mcp/client.py':b'RUNTIME',
                   SITE+'xuloom/__pycache__/engine.pyc':b'old-cache'}
            for name,data in files.items():
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            items=policy['retirements'](root,[SITE+'xuloom'])
            self.assertEqual([i['path'] for i in items],[SITE+'pymupdf/mupdf-devel/proof.h'])
            self.assertEqual(items[0]['sha256'],hashlib.sha256(b'header').hexdigest())
            self.assertEqual(items[0]['bytes'],6)
            for name,data in files.items():self.assertEqual((root/name).read_bytes(),data)

    def test_fresh_runtime_uses_same_policy_without_removing_runtime_or_license(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ['Lib/site-packages/pymupdf/mupdf-devel/proof.h','Lib/site-packages/PyWin32.chm',
                         'Lib/site-packages/pymupdf/mupdf-devel/COPYING','Lib/site-packages/pymupdf/_mupdf.pyd','python.exe']:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'kept or pruned')
            removed=policy['prune_generated_runtime'](root)
            self.assertEqual(len(removed),2)
            for name in ['Lib/site-packages/pymupdf/mupdf-devel/COPYING','Lib/site-packages/pymupdf/_mupdf.pyd','python.exe']:
                self.assertTrue((root/name).exists(),name)

    def test_default_and_optional_builder_write_standard_zip_without_extra_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.write_bytes(b'unchanged input')
            files=[(Path('resources/app/proof.txt'),source)]
            def fake_program(command,**options):
                self.assertIn('-mm=Deflate',command);self.assertIn('-mmt=2',command)
                cwd=options['cwd'];p=cwd/'Xuloom-win32-x64/resources/app/proof.txt'
                self.assertEqual(p.read_bytes(),source.read_bytes())
                with zipfile.ZipFile(command[-2],'x',compression=zipfile.ZIP_DEFLATED) as z:
                    for f in (cwd/'Xuloom-win32-x64').rglob('*'):
                        if f.is_file():z.write(f,f.relative_to(cwd).as_posix())
            for tool in [None,source]:
                output=root/('default.zip' if tool is None else 'optional.zip')
                with patch('subprocess.run',side_effect=fake_program):release['write_archive'](output,files,{'tag':'fixture'},tool)
                with zipfile.ZipFile(output) as z:
                    self.assertIsNone(z.testzip())
                    self.assertEqual(set(z.namelist()),{'Xuloom-win32-x64/resources/app/proof.txt','Xuloom-win32-x64/RELEASE.json'})
                    self.assertTrue(all(i.compress_type in {0,8} for i in z.infolist()))
                    z.extractall(root/(output.stem+'-extracted'))
                with self.assertRaises((FileExistsError,ValueError)):release['write_archive'](output,files,{},tool)
            self.assertEqual(source.read_bytes(),b'unchanged input')
            with self.assertRaises(ValueError):release['write_archive'](root/'bad.zip',[(Path('../escape'),source)],{})
            for invalid in [[(Path('RELEASE.json'),source)],files+files]:
                with self.assertRaises(ValueError):release['write_archive'](root/'bad.zip',invalid,{},source)
            self.assertEqual(source.read_bytes(),b'unchanged input')


@unittest.skipUnless(os.name=='nt', 'Windows updater')
class PrunedUpdateTests(unittest.TestCase):
    setUp=distribution.DistributionTests.setUp
    tearDown=distribution.DistributionTests.tearDown
    write_manifest=distribution.DistributionTests.write_manifest
    update=distribution.DistributionTests.update

    def candidate(self):
        p=self.package/(SITE+'pymupdf/mupdf-devel/proof.h');p.parent.mkdir(parents=True);p.write_bytes(b'development header')
        self.manifest['retiredFiles']=policy['retirements'](self.package)
        self.write_manifest();return p

    def test_retired_files_roll_back_with_failed_update_and_retry_is_safe(self):
        candidate=self.candidate()
        failed=self.update(fault=True);self.assertNotEqual(failed.returncode,0,failed.stderr)
        self.assertEqual(candidate.read_bytes(),b'development header')
        self.assertEqual((self.package/'resources/app/marker').read_text(),'old-app')
        success=self.update();self.assertEqual(success.returncode,0,success.stderr)
        self.assertFalse(candidate.exists())
        self.assertEqual((self.stage/'backup/retired-resources'/candidate.relative_to(self.package)).read_bytes(),b'development header')
        self.assertEqual((self.data/'history.json').read_text(),'PRIVATE-HISTORY-KEPT')

    def test_changed_file_and_forbidden_targets_are_rejected_before_any_replacement(self):
        candidate=self.candidate();candidate.write_bytes(b'changed after staging')
        result=self.update();self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.package/'resources/app/marker').read_text(),'old-app')
        for name in ['../user-data/history.json','resources/python/Lib/site-packages/pymupdf/_mupdf.pyd',
                     'resources/python/Lib/site-packages/pymupdf/LICENSE.txt']:
            self.manifest['retiredFiles']=[{'path':name,'sha256':'0'*64}];self.write_manifest()
            result=self.update();self.assertNotEqual(result.returncode,0,name)
            self.assertEqual((self.package/'resources/app/marker').read_text(),'old-app')
