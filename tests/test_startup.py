"""Fresh-process import contracts; help and empty sessions never initialize providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StartupTests(unittest.TestCase):
    def child(self, script, *args):
        with tempfile.TemporaryDirectory() as folder:
            env = {k:v for k,v in os.environ.items() if not k.startswith(('XULOOM_', 'OPENAI_', 'ANTHROPIC_'))}
            env.update(XULOOM_CONFIG_DIR=folder+'/config', XULOOM_DATA_DIR=folder+'/data', HOME=folder+'/home', USERPROFILE=folder+'/home',
                PYTHONPATH=os.environ.get('XULOOM_TEST_SITE', str(Path(__file__).resolve().parents[1])))
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, *args], env=env, cwd=folder,
                text=True, encoding='utf-8', capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout.strip().splitlines()[-1])

    def test_all_help_entries_avoid_engine_and_provider_or_document_sdks(self):
        script = '''
import contextlib,io,json,runpy,sys
sys.argv=['xuloom',*sys.argv[1:]]
with contextlib.redirect_stdout(io.StringIO()):
    try: runpy.run_module('xuloom',run_name='__main__')
    except SystemExit as e: assert e.code==0
print(json.dumps({n:n in sys.modules for n in ('xuloom.core.engine','anthropic','openai','mcp','pymupdf','playwright','paramiko','xuloom.scaling.control')}))
'''
        for args in [('--help',), ('exec','--help'), ('scaling','--help'), ('mcp','--help'), ('remote','--help')]:
            with self.subTest(args=args): self.assertFalse(any(self.child(script, *args).values()))

    def test_unconfigured_engine_opens_and_closes_without_provider_mcp_or_pdf_imports(self):
        self.assertFalse(any(self.child('''
import asyncio,json,sys
from xuloom.core.engine import QueryEngine
async def check():
    async with QueryEngine() as engine:
        assert not engine.is_configured
        assert engine.client is None and engine.compactor.client is None
asyncio.run(check())
print(json.dumps({n:n in sys.modules for n in ('anthropic','openai','mcp','pymupdf','playwright','paramiko','xuloom.scaling.control')}))
''').values()))

    def test_selected_openai_connection_does_not_import_other_provider_mcp_or_pdf(self):
        self.assertEqual(self.child('''
import asyncio,json,sys
from xuloom.core.engine import QueryEngine
async def check():
    async with QueryEngine(model='fixture',model_provider='openai',api_key='fixture',api_base_url='http://127.0.0.1:9/v1') as engine:
        assert engine.is_configured
asyncio.run(check())
print(json.dumps({n:n in sys.modules for n in ('anthropic','openai','mcp','pymupdf','playwright','paramiko','xuloom.scaling.control')}))
'''), {'anthropic':False,'openai':True,'mcp':False,'pymupdf':False,'playwright':False,'paramiko':False,'xuloom.scaling.control':False})
