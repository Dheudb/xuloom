import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rich.console import Console
from xuloom.features.references import file_reference
from xuloom.features.workspace_files import inspect_reference
from xuloom.terminal_markdown import TerminalMarkdown, MarkdownStream


class ReferenceTests(unittest.TestCase):
    def test_shared_contract(self):
        cases = json.loads((Path(__file__).parent/'fixtures/reference-cases.json').read_text(encoding='utf-8'))
        for item in cases:
            with self.subTest(value=item['value']):
                self.assertEqual(file_reference(item['value'], item.get('inline', False)), item['expected'])

    def test_previews_enforce_workspace_and_base_file(self):
        with tempfile.TemporaryDirectory() as fixture:
            root=(Path(fixture)/'root').resolve(); root.mkdir(); folder=root/'docs'; folder.mkdir()
            (folder/'README').write_text('one\ntwo\n'); (folder/'index.md').write_text('index')
            outside=Path(fixture)/'private'; outside.write_text('secret')
            self.assertEqual(inspect_reference(root,'README',base_path='docs/index.md')['path'],str(folder/'README'))
            self.assertTrue(inspect_reference(root,'docs')['directory'])
            for path in ['../private',str(outside),'missing']:
                with self.assertRaises(ValueError): inspect_reference(root,path)
            try: (root/'escape').symlink_to(outside)
            except OSError: return
            with self.assertRaises(ValueError): inspect_reference(root,'escape')

    def test_terminal_hyperlinks_and_readable_fallback(self):
        with tempfile.TemporaryDirectory() as fixture:
            root=Path(fixture).resolve(); (root/'README').write_text('one\ntwo')
            text='[Source](https://example.org/paper) and [file](README:2). `README` https://example.org/bare [bad](javascript:42) [escape](../private)'
            for links in [True,False]:
                stream=io.StringIO(); console=Console(file=stream,force_terminal=True,color_system='standard',legacy_windows=False,width=200)
                console.print(TerminalMarkdown(text,root,links=links)); output=stream.getvalue()
                self.assertIn('https://example.org/paper',output)
                self.assertIn((root/'README').as_uri(),output)
                self.assertIn('https://example.org/bare',output)
                self.assertNotIn(';javascript:',output); self.assertNotIn((root.parent/'private').as_uri(),output)
                self.assertEqual('\x1b]8;' in output,links)

    def test_stream_keeps_split_links_fences_and_reference_definitions(self):
        stream=io.StringIO(); console=Console(file=stream,width=100,color_system=None)
        renderer=MarkdownStream(console,'.',links=False)
        for chunk in ['[Source](https://exam','ple.org/a)\n','\n','```python\nx = 1\n\n','x += 2\n```\n\n']:
            renderer.append(chunk)
        renderer.append('[Later][id]\n\n'); self.assertNotIn('Later',stream.getvalue())
        renderer.append('[id]: https://example.org/later\n'); renderer.finish()
        self.assertIn('https://example.org/a',stream.getvalue()); self.assertIn('https://example.org/later',stream.getvalue())
        self.assertIn('x += 2',stream.getvalue()); self.assertNotIn('```',stream.getvalue())
        self.assertEqual(stream.getvalue().count('Source'),1)

    def test_remote_paths_are_never_reinterpreted_as_local_files(self):
        stream=io.StringIO(); console=Console(file=stream,force_terminal=True,color_system='standard',legacy_windows=False,width=200)
        with patch('xuloom.terminal_markdown.inspect_reference', side_effect=AssertionError('No local filesystem access for SSH output')):
            console.print(TerminalMarkdown('[remote file](/tmp/README:3) `C:/private/a.py` [web](https://example.org)', '/tmp',links=True,local_files=False))
        output=stream.getvalue()
        self.assertIn('/tmp/README:3',output); self.assertNotIn('file://',output)
        self.assertIn('https://example.org',output)

    def test_known_chat_links_remain_available_without_enabling_other_schemes(self):
        from xuloom.features.references import chat_reference
        url='xuloom://chat/v1/0123456789abcdef/saved-session'
        self.assertEqual(chat_reference(url),url)
        for value in [url+'?command=anything','xuloom://run/command',url+'\n']:
            self.assertIsNone(chat_reference(value))
        stream=io.StringIO(); console=Console(file=stream,force_terminal=True,color_system='standard',legacy_windows=False)
        console.print(TerminalMarkdown('[saved chat]('+url+')','.'))
        self.assertIn(url,stream.getvalue())

    def test_cli_receives_compaction_lifecycle_and_streams_markdown(self):
        from xuloom.main import InteractiveTerminalTUI
        class Engine:
            session_cwd='.'
            def get_mode(self): return 'normal'
        stream=io.StringIO(); console=Console(file=stream,color_system=None,width=120)
        tui=InteractiveTerminalTUI(console,lambda:Engine()); tui._stream=io.StringIO()
        tui.track_stream_event({'type':'context_compaction','id':'one','status':'running'})
        self.assertTrue(tui.compacting)
        tui.track_stream_event({'type':'context_compaction','id':'one','status':'completed'})
        tui.track_stream_event({'type':'context_compaction','id':'one','status':'completed'})
        self.assertFalse(tui.compacting); self.assertEqual(stream.getvalue().count('上下文已自动压缩'),1)
        tui.track_stream_event({'type':'context_compaction','id':'two','status':'failed'})
        self.assertIn('原始记录已保留',stream.getvalue())
        tui.track_stream_event({'type':'text_delta','text':'[Link](https://example.org)\n\n'})
        tui.stop_streaming(); self.assertIn('https://example.org',stream.getvalue()); self.assertNotIn('[Link](',stream.getvalue())

    def test_actual_interactive_cli_uses_local_protocol_and_prints_reply_once(self):
        import http.server
        import threading
        import subprocess
        import os
        import sys
        import xuloom
        requests=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                answer='[CLI_SOURCE_PROOF](https://example.org/paper)\n\n`README:2`\n\n'
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                for delta,reason in [({'role':'assistant','content':answer},None),({},'stop')]:
                    self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':reason}]})+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as fixture:
                root=Path(fixture).resolve(); (root/'README').write_text('one\ntwo')
                home=root/'home'; home.mkdir(); config=root/'config'; config.mkdir()
                (config/'config.json').write_text(json.dumps({'model_provider':'openai','openai_model':'local-fixture','openai_api_key':'fixture',
                    'openai_base_url':f'http://127.0.0.1:{server.server_address[1]}/v1'}),encoding='utf-8')
                env={k:v for k,v in os.environ.items() if not k.startswith(('OPENAI_','ANTHROPIC_','XULOOM_'))}
                env.update(HOME=str(home),USERPROFILE=str(home),XULOOM_CONFIG_DIR=str(config),XULOOM_DATA_DIR=str(root/'data'),
                    XULOOM_HYPERLINKS='0',PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONPATH=str(Path(xuloom.__file__).resolve().parent.parent))
                run=subprocess.run([sys.executable,'-u','-m','xuloom','-p','local rendering check'],input='/exit\n',cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
                self.assertEqual(run.returncode,0,run.stderr[-2000:])
                self.assertEqual(run.stdout.count('CLI_SOURCE_PROOF'),1,run.stdout[-2500:])
                self.assertIn('https://example.org/paper',run.stdout)
                self.assertIn((root/'README').as_uri()+'#L2',run.stdout)
                self.assertEqual(len(requests),1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
