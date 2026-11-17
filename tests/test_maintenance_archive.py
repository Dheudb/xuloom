import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from xuloom.core.session_lock import SessionBusyError, SessionLock
from xuloom.maintenance.archive import backup, diagnostic, inspect_backup, restore


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'data'
        (self.root / 'history/a').mkdir(parents=True)
        (self.root / 'history/a/index.json').write_text(json.dumps({'sessions': {'id': {'title': 'private title'}}}))
        (self.root / 'workspaces/default').mkdir(parents=True)
        (self.root / 'workspaces/default/file.txt').write_text('owned content')
        self.zip = self.base / 'backup.zip'

    def create(self):
        return backup(self.root, self.zip, {'model_connections': [{'id':'a','provider':'openai','api_key':'private-secret','model':'m','models':['m']}]})

    def test_restore_preserves_rollback_and_does_not_copy_credentials(self):
        self.create()
        manifest = inspect_backup(self.zip)
        self.assertNotIn('private-secret', json.dumps(manifest))
        source = self.root / 'workspaces/default/file.txt'
        source.write_text('new data')
        with self.assertRaises(ValueError):
            restore(self.root, self.zip)
        result = restore(self.root, self.zip, confirmed=True)
        self.assertEqual(source.read_text(), 'owned content')
        self.assertEqual((Path(result['rollback']) / 'workspaces/default/file.txt').read_text(), 'new data')

    def test_busy_writer_and_maintenance_lease_block_restore_or_new_writer(self):
        self.create()
        lease = SessionLock(self.root / 'session-locks', 'active')
        lease.acquire()
        try:
            with self.assertRaises(SessionBusyError):
                restore(self.root, self.zip, confirmed=True)
        finally:
            lease.close()
        gate = SessionLock(self.root / 'session-locks', 'maintenance')
        gate.acquire()
        try:
            with self.assertRaises(SessionBusyError):
                SessionLock(self.root / 'session-locks', 'new').acquire()
        finally:
            gate.close()

    def test_corrupt_backup_rejected_before_data_mutation(self):
        self.create()
        with zipfile.ZipFile(self.zip) as source:
            files = {name: source.read(name) for name in source.namelist()}
        files['workspaces/default/file.txt'] = b'tampered'
        with zipfile.ZipFile(self.zip, 'w') as target:
            for name, value in files.items():
                target.writestr(name, value)
        with self.assertRaises(ValueError):
            restore(self.root, self.zip, confirmed=True)
        self.assertEqual((self.root / 'workspaces/default/file.txt').read_text(), 'owned content')

    def test_partial_install_failure_rolls_back(self):
        self.create()
        real = Path.rename
        def fail(source, target):
            if source.name == 'workspaces' and source.parent.name.startswith('.xuloom-restore-'):
                raise OSError('simulated disk failure')
            return real(source, target)
        with patch.object(Path, 'rename', fail), self.assertRaises(OSError):
            restore(self.root, self.zip, confirmed=True)
        self.assertEqual((self.root / 'workspaces/default/file.txt').read_text(), 'owned content')
        self.assertTrue((self.root / 'history/a/index.json').exists())

    def test_diagnostics_excludes_user_content(self):
        out = self.base / 'diagnostic.json'
        value = diagnostic(self.root, out)
        self.assertEqual(value['counts']['sessions'], 1)
        self.assertNotIn('private', out.read_text())
        self.assertNotIn(str(self.root), out.read_text())

    def test_zip_traversal_is_rejected(self):
        with zipfile.ZipFile(self.zip, 'w') as out:
            out.writestr('../outside', b'test')
            out.writestr('BACKUP.json', json.dumps({'format':'xuloom-backup-1','files':{'../outside':'x'}}))
        with self.assertRaises(ValueError):
            inspect_backup(self.zip)

    def test_live_backup_change_is_rejected(self):
        from xuloom.maintenance.archive import inventory
        with patch('xuloom.maintenance.archive.inventory', side_effect=[inventory(self.root), {}]), self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.zip.exists())

    def test_settings_import_keeps_key_with_original_route(self):
        from xuloom.core import config
        from xuloom.maintenance.archive import import_settings
        config_file, price_file = self.base / 'config.json', self.base / 'pricing.json'
        config_file.write_text(json.dumps({'model_connections':[{'id':'a','provider':'openai','base_url':'https://original.invalid',
            'api_key':'local-key','model':'m','models':['m']}]}))
        (self.root / 'restored-settings.json').write_text(json.dumps({'connections':{'model_connections':[
            {'id':'a','provider':'openai','base_url':'https://other.invalid','api_key':'imported-secret','model':'m','models':['m']}]},
            'pricing':{'models':{'@a/m':{'input':1,'output':2}}}}))
        with patch.object(config, 'CONFIG_FILE', config_file), patch('xuloom.observability.cost.pricing_path', return_value=price_file):
            import_settings(self.root)
            cfg = config.load_config()
        original, imported = cfg['model_connections']
        self.assertEqual(original['api_key'], 'local-key')
        self.assertEqual(imported['api_key'], '')
        self.assertNotEqual(original['id'], imported['id'])
        self.assertIn('@' + imported['id'] + '/m', json.loads(price_file.read_text())['models'])
        self.assertFalse((self.root / 'restored-settings.json').exists())

    def test_restore_into_another_directory_rebases_owned_history(self):
        import hashlib
        import os
        old_cwd = str(self.root / 'workspaces/default')
        old_hash = hashlib.sha256(os.path.normcase(old_cwd).encode()).hexdigest()[:16]
        (self.root / 'history/a').rename(self.root / 'history' / old_hash)
        (self.root / 'history' / old_hash / 'project.json').write_text(json.dumps({'project': old_cwd}))
        (self.root / 'workbench.json').write_text(json.dumps({'standaloneDirectories':[old_cwd], 'projects':[old_cwd],
            'projectMeta':{old_cwd:{'name':'keep'}}, 'activeSession':old_hash+':id'}))
        self.create()
        target = self.base / 'restored'
        restore(target, self.zip, confirmed=True)
        new_cwd = str(target / 'workspaces/default')
        new_hash = hashlib.sha256(os.path.normcase(new_cwd).encode()).hexdigest()[:16]
        preferences = json.loads((target / 'workbench.json').read_text())
        self.assertEqual([os.path.normcase(v) for v in preferences['standaloneDirectories']], [os.path.normcase(new_cwd)])
        self.assertEqual(preferences['activeSession'], new_hash+':id')
        self.assertIn(os.path.normcase(new_cwd), preferences['projectMeta'])
        self.assertTrue((target / 'history' / new_hash / 'index.json').exists())
