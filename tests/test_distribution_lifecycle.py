"""Resource updates in throwaway installations; never touch the user's executable."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform=='win32' and shutil.which('pwsh'),'Windows resource updater')
class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.package=self.root/'app';self.stage=self.root/'stage';self.data=self.root/'user-data'
        self.site=self.package/'resources/python/Lib/site-packages'
        for path in (self.package/'resources/app',self.site/'xuloom',self.site/'xuloom-0.0.1.dist-info',self.stage/'app',self.stage/'xuloom',self.stage/'metadata',self.data):path.mkdir(parents=True)
        self.exe=self.package/'Xuloom.exe';self.exe.write_bytes(b'fixture; never executed')
        (self.package/'resources/app/marker').write_text('old-app')
        (self.site/'xuloom/marker').write_text('old-engine')
        (self.site/'xuloom-0.0.1.dist-info/METADATA').write_text('old-metadata')
        for name in ('app','xuloom','metadata'):(self.stage/name/'marker').write_text('new-'+name)
        (self.data/'history.json').write_text('PRIVATE-HISTORY-KEPT')
        self.manifest={'metadataDirectory':'xuloom-0.0.2.dist-info','pythonPackages':[],
            'originalExeSha256':hashlib.sha256(self.exe.read_bytes()).hexdigest(),
            'resourceHashes':{p.relative_to(self.stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.stage.rglob('*') if p.is_file()}}
        self.write_manifest()

    def tearDown(self):self.temp.cleanup()
    def write_manifest(self):(self.stage/'manifest.json').write_text(json.dumps(self.manifest))
    def update(self, fault=False):
        script=Path(__file__).resolve().parents[1]/'desktop/scripts/update-existing.ps1'
        if fault:
            # Inject a filesystem failure after app replacement without adding a production fault switch.
            wrapper=self.root/'fault.ps1'
            wrapper.write_text('''function Move-Item {
param([string]$LiteralPath,[string]$Destination)
if ($LiteralPath -eq $env:XULOOM_TEST_FAIL_SOURCE) { throw 'Injected resource move failure' }
Microsoft.PowerShell.Management\\Move-Item -LiteralPath $LiteralPath -Destination $Destination
}
& $env:XULOOM_TEST_UPDATE -PackageDirectory $env:XULOOM_TEST_PACKAGE -StagedResources $env:XULOOM_TEST_STAGE -WaitSeconds 0
exit $LASTEXITCODE
''')
            env={**os.environ,'XULOOM_TEST_UPDATE':str(script),'XULOOM_TEST_PACKAGE':str(self.package),
                'XULOOM_TEST_STAGE':str(self.stage),'XULOOM_TEST_FAIL_SOURCE':str(self.stage/'xuloom')}
            return subprocess.run(['pwsh','-NoProfile','-File',str(wrapper)],env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
        return subprocess.run(['pwsh','-NoProfile','-File',str(script),'-PackageDirectory',str(self.package),'-StagedResources',str(self.stage),'-WaitSeconds','0'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)

    def test_update_keeps_launcher_data_and_rollback_and_retire_old_metadata(self):
        result=self.update();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.package/'resources/app/marker').read_text(),'new-app')
        self.assertEqual((self.stage/'backup/app/marker').read_text(),'old-app')
        self.assertFalse((self.site/'xuloom-0.0.1.dist-info').exists())
        self.assertTrue((self.site/'xuloom-0.0.2.dist-info').exists())
        self.assertEqual(hashlib.sha256(self.exe.read_bytes()).hexdigest(),self.manifest['originalExeSha256'])
        shutil.rmtree(self.package)
        self.assertEqual((self.data/'history.json').read_text(),'PRIVATE-HISTORY-KEPT','Deleting portable application keeps separately stored user data')

    def test_invalid_update_is_rejected_before_mutation(self):
        (self.stage/'app/marker').write_text('tampered')
        result=self.update();self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.package/'resources/app/marker').read_text(),'old-app')
        self.assertEqual(json.loads((self.stage/'status.json').read_text(encoding='utf-8-sig'))['state'],'failed')

    def test_mid_update_failure_rolls_back_already_replaced_application(self):
        result=self.update(fault=True);self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual((self.package/'resources/app/marker').read_text(),'old-app')
        self.assertEqual((self.site/'xuloom/marker').read_text(),'old-engine')
        self.assertTrue((self.site/'xuloom-0.0.1.dist-info/METADATA').exists())
        self.assertEqual(json.loads((self.stage/'status.json').read_text(encoding='utf-8-sig'))['state'],'failed')
