import json
import queue
import subprocess
import sys
import threading
import unittest
import uuid
from tests import test_noninteractive_cli as existing


class SteeringCliTests(unittest.TestCase):
    setUp=existing.CliTests.setUp
    tearDown=existing.CliTests.tearDown

    def start(self,*args):
        process=subprocess.Popen([sys.executable,'-X','utf8','-m','xuloom','exec',*args,'--cwd',str(self.root),'--json'],
            env=self.env,cwd=self.root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        events=queue.Queue();all_events=[]
        def read():
            for line in process.stdout:
                item=json.loads(line);all_events.append(item);events.put(item)
        thread=threading.Thread(target=read,daemon=True);thread.start()
        return process,events,all_events,thread

    def until(self,events,kind):
        for _ in range(100):
            event=events.get(timeout=15)
            if event['type']==kind:return event
        self.fail('Missing '+kind)

    def close(self,process,thread):
        if process.poll() is None:process.kill()  # Only the explicitly owned CLI fixture.
        process.wait(5);thread.join(3)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def test_open_stdin_does_not_block_completion_and_input_is_delivered_once(self):
        self.mode='hold';process,events,all_events,thread=self.start('ORIGINAL','--input-stream')
        try:
            self.assertTrue(self.request_started.wait(15));identity=str(uuid.uuid4())
            process.stdin.write('not-json\n'+json.dumps({'text':'追加方向','request_id':identity})+'\n');process.stdin.flush()
            self.until(events,'input_error');self.until(events,'user_input_queued')
            process.stdin.write(json.dumps({'text':'追加方向','request_id':identity})+'\n');process.stdin.flush()
            self.until(events,'user_input_queued');self.finish_request.set()
            final=self.until(events,'final');process.wait(10)
            self.assertEqual(process.returncode,0,process.stderr.read())
            self.assertTrue(final['result']['ok']);self.assertEqual(len(self.requests),2)
            delivery=[e for e in all_events if e['type']=='user_input_applied'];self.assertEqual(len(delivery),1)
            self.assertEqual(len(delivery[0]['inputs']),1);self.assertIn('追加方向',json.dumps(self.requests[-1],ensure_ascii=False))
        finally:self.close(process,thread)

    def test_stop_preserves_pending_input_and_formal_resume_needs_no_prompt_stdin(self):
        self.mode='hold';process,events,_,thread=self.start('ORIGINAL','--input-stream')
        try:
            self.assertTrue(self.request_started.wait(15));process.stdin.write(json.dumps({'text':'AFTER_RESTART'})+'\n');process.stdin.flush()
            self.until(events,'user_input_queued');process.stdin.write('{"type":"stop"}\n');process.stdin.flush()
            final=self.until(events,'final');process.wait(10);self.assertEqual(process.returncode,130)
            identity=final['result']['session_id']
        finally:self.close(process,thread)
        self.finish_request.set();self.mode='text'
        process,events,_,thread=self.start('--resume','--session',identity,'--input-stream')
        try:
            final=self.until(events,'final');process.wait(10)
            self.assertEqual(process.returncode,0,process.stderr.read());self.assertTrue(final['result']['ok'])
            self.assertIn('AFTER_RESTART',json.dumps(self.requests[-1]))
        finally:self.close(process,thread)
