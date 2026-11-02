import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from xuloom.core.session_lock import SessionBusyError, SessionLock
from xuloom.security import sandbox_cleanup as cleanup


@unittest.skipUnless(os.name == "nt", "Windows AppContainer cleanup")
class SandboxCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR":str(self.root/'data')})
        self.env.start()
        from xuloom.security.windows_sandbox import WindowsAPI
        self.api = WindowsAPI()
        identity = uuid.uuid4().hex
        self.report = dict(id=identity, profile='xuloom.sandbox.'+identity, workspace=str(self.root.resolve()),
            sid=None, grants=[str(self.root)], traversed=[], status='active', failures=[])
        sid = ctypes.c_void_p()
        result = self.api.userenv.CreateAppContainerProfile(self.report['profile'], self.report['profile'], 'Xuloom owned cleanup test', None, 0, ctypes.byref(sid))
        self.assertGreaterEqual(result, 0)
        text = wintypes.LPWSTR()
        self.api.check(self.api.advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        self.report['sid'] = text.value
        self.api.kernel.LocalFree(text); self.api.advapi.FreeSid(sid)
        cleanup.save(self.report)

    def tearDown(self):
        self.api.userenv.DeleteAppContainerProfile(self.report['profile'])
        self.env.stop(); self.temp.cleanup()

    def test_acl_failure_is_durable_and_explicit_retry_does_not_repeat_execution(self):
        import subprocess
        with patch.object(cleanup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 5)):
            report = cleanup.remove_grants(self.report, self.api, 'icacls')
        self.assertEqual(report['status'], 'cleanup_failed')
        self.assertEqual(report['failures'][0]['operation'], 'remove_acl')
        self.assertEqual(cleanup.pending(self.root)[0]['id'], report['id'])
        with patch.object(cleanup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as command:
            result = cleanup.retry(self.root, report['id'])
        self.assertEqual(result['status'], 'cleaned')
        self.assertTrue(all('/remove' in call.args[0] for call in command.call_args_list))
        self.assertEqual(cleanup.pending(self.root), [])

    def test_profile_delete_failure_and_active_lease_are_not_silenced(self):
        from types import SimpleNamespace
        proxy = SimpleNamespace(advapi=self.api.advapi,kernel=self.api.kernel,check=self.api.check,traverse=self.api.traverse,
            userenv=SimpleNamespace(DeriveAppContainerSidFromAppContainerName=self.api.userenv.DeriveAppContainerSidFromAppContainerName,
                DeleteAppContainerProfile=lambda name:-2147024891))
        self.report['grants'] = []
        result = cleanup.remove_grants(self.report, proxy, 'icacls')
        self.assertEqual(result['failures'][0]['operation'], 'delete_profile')
        lock = SessionLock(cleanup.directory(), result['id']); lock.acquire()
        try:
            self.assertEqual(cleanup.pending(self.root), [])
            with self.assertRaises(SessionBusyError): cleanup.retry(self.root,result['id'])
        finally: lock.close()
        self.assertEqual(len(cleanup.pending(self.root)),1)

    def test_changed_sid_or_workspace_is_rejected_without_removing_any_grant(self):
        self.report['sid'] = 'S-1-15-2-1-2-3-4-5-6-7'
        cleanup.save(self.report)
        with patch.object(cleanup.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError,'does not match'): cleanup.retry(self.root,self.report['id'])
            command.assert_not_called()
        with self.assertRaisesRegex(ValueError,'another workspace'):
            cleanup.retry(self.root/'another',self.report['id'])
        self.assertEqual(json.loads(cleanup.report_path(self.report['id']).read_text())['sid'], self.report['sid'])

    def test_real_owned_acl_can_be_recovered_after_profile_deletion(self):
        import subprocess
        from xuloom.security.execution import ExecutionSandbox, SandboxUnavailable
        icacls = str(Path(os.environ['SystemRoot'])/'System32/icacls.exe')
        sid = self.report['sid']
        result = subprocess.run([icacls,str(self.root),'/L','/grant','*'+sid+':RX'],capture_output=True)
        self.assertEqual(result.returncode,0)
        try:
            with patch.object(cleanup.subprocess,'run',return_value=subprocess.CompletedProcess([],5)):
                cleanup.remove_grants(self.report,self.api,icacls)
            with self.assertRaisesRegex(SandboxUnavailable,'needs permission cleanup'):
                ExecutionSandbox(self.root,'workspace').prepare(argv=['cmd','/c','echo no'])
            result = cleanup.retry(self.root,self.report['id'])
            self.assertEqual(result['status'],'cleaned')
            raw = subprocess.run([icacls,str(self.root),'/L'],capture_output=True)
            self.assertNotIn(sid.encode(),raw.stdout)
            self.assertEqual(cleanup.pending(self.root),[])
        finally:
            subprocess.run([icacls,str(self.root),'/L','/remove','*'+sid],capture_output=True)

    def test_windows_workspace_case_does_not_hide_a_cleanup_failure(self):
        self.report['workspace'] = str(self.root).upper()
        cleanup.save(self.report)
        self.assertEqual(cleanup.pending(self.root)[0]['id'],self.report['id'])
        self.assertEqual(cleanup.retry(self.root,self.report['id'])['status'],'cleaned')

    def test_missing_sid_does_not_skip_recorded_permission_removal(self):
        import subprocess
        expected=self.report['sid'];self.report['sid']=None
        with patch.object(cleanup.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as command:
            result=cleanup.remove_grants(self.report,self.api,'icacls')
        self.assertEqual(result['status'],'cleaned')
        self.assertTrue(any('*'+expected in call.args[0] for call in command.call_args_list), 'derive the owned SID instead of ignoring grants')

    def test_another_launch_temporary_directory_is_never_deleted(self):
        with tempfile.TemporaryDirectory(prefix='xuloom-restricted-'+uuid.uuid4().hex+'-') as foreign:
            marker=Path(foreign)/'keep.txt';marker.write_text('KEEP')
            self.report.update(grants=[],scratch=foreign)
            with patch.object(cleanup.shutil,'rmtree') as remove:
                result=cleanup.remove_grants(self.report,self.api,'icacls')
                self.assertEqual(result['status'],'cleanup_failed')
                remove.assert_not_called()
            self.assertEqual(marker.read_text(),'KEEP')

    def test_legacy_writable_acl_proves_ownership_and_failed_delete_can_be_retried(self):
        import subprocess
        icacls=str(Path(os.environ['SystemRoot'])/'System32/icacls.exe')
        with tempfile.TemporaryDirectory(prefix='xuloom-restricted-legacy-') as scratch:
            sid=self.report['sid']
            result=subprocess.run([icacls,scratch,'/L','/grant','*'+sid+':(OI)(CI)M'],capture_output=True)
            self.assertEqual(result.returncode,0)
            self.report.update(scratch=scratch,grants=[scratch])
            with patch.object(cleanup.shutil,'rmtree',side_effect=PermissionError('owned fixture')):
                result=cleanup.remove_grants(self.report,self.api,icacls)
            self.assertEqual(result['status'],'cleanup_failed')
            self.assertEqual(result['failures'][0]['operation'],'remove_scratch')
            self.assertEqual(result['scratch_identity'],cleanup.scratch_identity(scratch))
            self.assertEqual(cleanup.retry(self.root,result['id'])['status'],'cleaned')
            self.assertFalse(Path(scratch).exists())

    def test_read_only_acl_is_not_proof_of_scratch_ownership(self):
        import subprocess
        icacls=str(Path(os.environ['SystemRoot'])/'System32/icacls.exe')
        with tempfile.TemporaryDirectory(prefix='xuloom-restricted-readonly-') as folder:
            result=subprocess.run([icacls,folder,'/L','/grant','*'+self.report['sid']+':(OI)(CI)RX'],capture_output=True)
            self.assertEqual(result.returncode,0)
            self.report.update(scratch=folder,grants=[folder])
            with patch.object(cleanup.shutil,'rmtree') as remove:
                result=cleanup.remove_grants(self.report,self.api,icacls)
                self.assertEqual(result['status'],'cleanup_failed')
                remove.assert_not_called()
            self.assertTrue(Path(folder).is_dir())

    def test_replaced_directory_and_malformed_paths_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix='xuloom-restricted-'+self.report['id']+'-') as folder:
            identity=cleanup.scratch_identity(folder)
            self.report.update(grants=[],scratch=folder,scratch_identity=[identity[0],identity[1]+1])
            with patch.object(cleanup.shutil,'rmtree') as remove:
                result=cleanup.remove_grants(self.report,self.api,'icacls')
                self.assertEqual(result['status'],'cleanup_failed')
                remove.assert_not_called()
        self.report['grants']=['../foreign']
        with patch.object(self.api.userenv,'DeleteAppContainerProfile') as remove:
            with self.assertRaisesRegex(ValueError,'cleanup paths'):
                cleanup.remove_grants(self.report,self.api,'icacls')
            remove.assert_not_called()

    def test_one_bad_record_has_a_visible_diagnostic_instead_of_breaking_status(self):
        identity=uuid.uuid4().hex
        path=cleanup.report_path(identity);raw='{broken JSON'
        path.write_text(raw,encoding='utf-8')
        issues=cleanup.pending(self.root)
        damaged=next(item for item in issues if item['id']==identity)
        self.assertEqual(damaged['status'],'record_invalid')
        self.assertEqual(damaged['report_path'],str(path))
        self.assertEqual(path.read_text(encoding='utf-8'),raw)
        self.assertTrue(any(item['id']==self.report['id'] for item in issues))
