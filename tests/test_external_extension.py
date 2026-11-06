"""Copy the public example outside the repository and exercise real SDK/HTTP/processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest


class ExternalExtensionTests(unittest.TestCase):
    def test_outside_project_tool_events_cancellation_and_restart_use_public_methods(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); project = root/'outside-project'; project.mkdir()
            source = Path(__file__).resolve().parents[1]
            shutil.copyfile(source/'examples/extension_project/app.py', project/'app.py')
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(body)
                    self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                    messages = json.dumps(body['messages'])
                    if 'ORIGINAL_HOLD_TASK' in messages:
                        delta = {'content':'RESUME_PASS'}
                    elif any(m['role']=='tool' for m in body['messages']): delta = {'content':'EXTENSION_PASS'}
                    else:
                        assert any(t['function']['name']=='evidence_tool' for t in body['tools'])
                        delta = {'tool_calls':[{'index':0,'id':'custom-evidence','type':'function',
                            'function':{'name':'evidence_tool','arguments':json.dumps({'value':'outside-project'})}}]}
                    try:
                        for content, finish in [(delta,None),({},'tool_calls' if 'tool_calls' in delta else 'stop')]:
                            self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':content,'finish_reason':finish}]})+'\n\n').encode())
                        self.wfile.write(b'data: [DONE]\n\n')
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError): pass
            server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            env={k:v for k,v in os.environ.items() if not k.startswith(('OPENAI_','ANTHROPIC_','XULOOM_'))}
            env.update(XULOOM_CONFIG_DIR=str(root/'config'),XULOOM_DATA_DIR=str(root/'data'),HOME=str(root/'home'),USERPROFILE=str(root/'home'),
                EXAMPLE_API_KEY='fixture',PYTHONPATH=os.environ.get('XULOOM_TEST_SITE',str(source)),PYTHONIOENCODING='utf-8')
            def run(*extra):
                result = subprocess.run([sys.executable,'-X','utf8','app.py','--base-url',f'http://127.0.0.1:{server.server_port}/v1',
                    '--model','external-compatible-model',*extra],cwd=project,env=env,text=True,encoding='utf-8',capture_output=True,timeout=25)
                self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                events=[json.loads(line) for line in result.stdout.splitlines()]
                finals=[e['result'] for e in events if e['type']=='final']
                self.assertEqual(len(finals),1,result.stdout)
                return events,finals[0]
            try:
                events,result=run()
                self.assertTrue(result['ok']); self.assertEqual(result['text'],'EXTENSION_PASS')
                self.assertTrue(any(e['type']=='tool_result' for e in events))
                self.assertTrue(all(r['model']=='external-compatible-model' for r in requests))
                _,cancelled=run('--cancel','ORIGINAL_HOLD_TASK')
                self.assertEqual(cancelled['reason'],'cancelled'); self.assertTrue(cancelled['persisted'])
                before=len(requests)
                _,resumed=run('--session',cancelled['session_id'],'--resume')
                self.assertTrue(resumed['ok']); self.assertEqual(resumed['text'],'RESUME_PASS')
                self.assertEqual(len(requests),before+1,'Restart resumes without replaying custom effects')
                self.assertIn('ORIGINAL_HOLD_TASK',json.dumps(requests[-1]['messages']))
            finally:
                server.shutdown(); server.server_close(); thread.join(3)
