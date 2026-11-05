import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xuloom.desktop.readiness import inspect
from xuloom.features.acceptance import validate,evaluate


class TaskReadinessTests(unittest.TestCase):
    def test_readiness_is_local_and_missing_program_is_not_a_success(self):
        with patch('xuloom.desktop.readiness.find_spec',return_value=None),patch('xuloom.desktop.readiness.shutil.which',return_value=None):
            result=inspect({'hasKey':True,'model':'fixture','apiKey':'DO-NOT-EXPOSE'},'workspace')
        self.assertFalse(result['model']['verified_connection']);self.assertFalse(result['network_tools_available'])
        self.assertNotIn('DO-NOT-EXPOSE',str(result));self.assertTrue(all(not r['available'] for r in result['formats']))
        self.assertFalse(result['office_preview']);self.assertTrue(all(not r['found'] for r in result['external_agents']))

    def test_installed_parser_does_not_claim_layout_or_real_connection_verified(self):
        with patch('xuloom.desktop.readiness.find_spec',return_value=object()),patch('xuloom.desktop.readiness.shutil.which',return_value='/fixture/program'):
            result=inspect({})
        self.assertTrue(all(not row['install_argv'] for row in result['formats']))
        self.assertFalse(result['model']['configured']);self.assertTrue(result['office_preview'])

    def test_declarative_json_csv_and_hash_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result'
            path.write_text('{"rows":2,"complete":true}',encoding='utf-8')
            self.assertTrue(evaluate(path,{'kind':'json_fields','path':'result','fields':{'rows':2,'complete':True}}))
            self.assertFalse(evaluate(path,{'kind':'json_fields','path':'result','fields':{'complete':1}}))
            path.write_text('{"nested":{"done":true}}',encoding='utf-8')
            self.assertFalse(evaluate(path,{'kind':'json_fields','path':'result','fields':{'nested':{'done':1}}}))
            self.assertTrue(evaluate(path,{'kind':'file_sha256','path':'result','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
            path.write_text('x,y\n1,2\n3,4\n',encoding='utf-8')
            check={'kind':'csv_columns','path':'result','columns':['x','y'],'min_rows':2}
            self.assertTrue(evaluate(path,check));path.write_text('x,y\n1\n',encoding='utf-8');self.assertFalse(evaluate(path,check))
            path.write_text('x,x\n1,2\n',encoding='utf-8');self.assertFalse(evaluate(path,check))

    def test_arbitrary_code_extra_fields_and_nonfinite_values_are_rejected(self):
        for check in [{'kind':'python','path':'a','code':'pass'}, {'kind':'file_exists','path':'a','exec':'no'},
                      {'kind':'json_fields','path':'a','fields':{'value':float('nan')}},
                      {'kind':'csv_columns','path':'a','columns':['a'],'min_rows':True}]:
            with self.assertRaises(ValueError):validate(check)
