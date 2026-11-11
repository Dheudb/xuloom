"""Fresh-process import contracts; help and empty sessions never initialize providers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StartupTests(unittest.TestCase):
    def test_engine_and_directory_isolation_work_without_git(self):
        self.assertEqual(self.child('''
import asyncio,json,os
os.environ['PATH']=''
os.environ['GIT_PYTHON_REFRESH']='error'
from xuloom import QueryEngine
from xuloom.security.sandbox import SandboxManager
async def check():
    async with QueryEngine() as engine:
        assert not engine.is_configured
    boundary=SandboxManager(cwd=os.getcwd())
    assert boundary._get_workspace_root()==(os.getcwd(),False)
asyncio.run(check())
print(json.dumps({'engineOpen':True}))
'''), {'engineOpen': True})

    def test_scaling_configuration_load_and_save_do_not_start_controller_or_model(self):
        result = self.child('''
import asyncio,json,os,sys
from pathlib import Path
from xuloom.desktop.bridge import DesktopBridge
async def check():
    bridge=DesktopBridge(lambda _:None,cwd=os.getcwd())
    assert (await bridge.dispatch('scaling.config',{}))['nodes']==[]
    row={'id':'local','location':'local','max_agents':100}
    saved=await bridge.dispatch('scaling.save',{'nodes':[row]})
    assert saved[0]['max_agents']==100
    assert (await bridge.dispatch('scaling.config',{}))['nodes']==saved
    await bridge.dispatch('config.get',{})
    try: await bridge.dispatch('scaling.save',{'nodes':[row,row]})
    except ValueError: pass
    else: raise AssertionError('Duplicate node accepted')
    assert (await bridge.dispatch('scaling.config',{}))['nodes']==saved
    assert bridge.engine is None
    assert not (Path(os.environ['XULOOM_DATA_DIR'])/'scaling').exists()
asyncio.run(check())
print(json.dumps({n:n in sys.modules for n in ('xuloom.scaling.control','anthropic','openai','paramiko')}))
''')
        self.assertFalse(any(result.values()), result)

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
