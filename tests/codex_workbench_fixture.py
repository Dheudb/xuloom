"""App and formal CLI share an actual installed Codex and owned Responses server."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
PROTOCOL = sys.stdout
sys.stdout = sys.stderr

import test_external_agents as fixtures
from test_codex_responses import ResponsesModel
from external_discovery_api import CatalogModel
from xuloom.core.config import save_config


async def main():
    fixture = fixtures.ExternalFixture(); await fixture.asyncSetUp()
    try:
        program = os.environ['XULOOM_TEST_CODEX_PROGRAM']
        folder = fixture.root / '.xuloom/agents'; folder.mkdir(parents=True)
        (folder / 'codex-reader.md').write_text('---\nname: codex-reader\ndescription: independent evidence\nbackend: codex\nbackend_program: '
            + program.replace('\\', '/') + '\nconnection: owned-responses\ntools: [Read]\n---\nRead supplied evidence.\n', encoding='utf8')
        config = fixture.root / 'config'; config.mkdir(exist_ok=True)
        save_config({'model_connections':[
            {'id':'owned-parent','name':'Owned parent','provider':'anthropic','model':fixtures.MODEL,'models':[fixtures.MODEL],
             'api_key':'fixture-only','base_url':fixture.base,'network_mode':'direct'},
            {'id':'owned-responses','name':'Owned Responses','provider':'openai','model':'owned-codex-model','models':['owned-codex-model'],
             'api_key':'fixture-only','base_url':fixture.base+'/v1','network_mode':'direct'}], 'active_model_connection':'owned-parent'})
        (config / 'config.json').write_bytes((fixture.root / 'config.json').read_bytes())
        model, parent = ResponsesModel('namespace'), CatalogModel()
        phase, worker_id = 'bootstrap', None
        counts = {'bootstrap':0,'app':0,'cli':0}
        def respond(handler, body):
            if model(handler, body): return True
            if handler.path.endswith('/messages/count_tokens') or body.get('stream') is not True: return parent(handler,body)
            counts[phase] += 1
            values = {'action':'spawn','subagent_type':'codex-reader','prompt':'Read evidence.txt'} if phase == 'bootstrap' else {
                'action':'followup','agent_id':worker_id,'prompt':'Summarize the evidence in '+phase}
            return parent.reply(handler, body, name='agent_tool' if counts[phase] == 1 else None,
                values=values,text=phase.upper()+'_CODEX_COMPLETE',identity=phase+'_'+str(counts[phase]))
        fixture.parent_handler = respond
        engine = fixture.engine()
        result = await engine.run(user_input='Read evidence with Codex',sys_print_callback=lambda _:None)
        assert result.ok,result
        row = engine.subagent_registry[0]; assert row['status']=='completed',row
        worker_id, identity = row['agent_id'], engine.session_id
        before = engine.session_manager.load_subagent_transcript(worker_id)['metadata']['external_agent']
        await engine.aclose()
        phase = 'app'
        print(json.dumps({'home':str(fixture.root),'project':str(fixture.root),'data':str(fixture.root/'data'),
            'config':str(config),'sessionId':identity,'workerId':worker_id,'baseUrl':fixture.base}),file=PROTOCOL,flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        after_app=engine.session_manager.load_subagent_transcript(worker_id)
        assert after_app['metadata']['last_run_result']['status']=='completed',after_app
        phase='cli'
        local_python = os.environ.get('XULOOM_TEST_LOCAL_PYTHON')
        command=[local_python,'-I','-B'] if local_python else [sys.executable]
        command += ['-m','xuloom','exec','--cwd',str(fixture.root),'--session',identity,'--json','--permission','full','Resume Codex in CLI']
        env={**os.environ,'XULOOM_CONFIG_DIR':str(config),'XULOOM_NETWORK_MODE':'direct',
            'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','ALL_PROXY':'http://127.0.0.1:1','NO_PROXY':'127.0.0.1,localhost,::1'}
        for key in list(env):
            if key.startswith(('OPENAI_','ANTHROPIC_')):env.pop(key)
        env.pop('XULOOM_MODEL_PROVIDER',None)
        child=await asyncio.create_subprocess_exec(*command,cwd=ROOT,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            **({'creationflags':0x08000000} if os.name=='nt' else {}))
        try: stdout,stderr=await asyncio.wait_for(child.communicate(),45)
        finally:
            if child.returncode is None:child.kill();await child.wait()
        rows=[json.loads(line) for line in stdout.splitlines() if line.strip()]
        assert child.returncode==0 and rows[-1]['result']['reason']=='completed',stderr.decode(errors='replace')[-1000:]
        assert 'CLI_CODEX_COMPLETE' in json.dumps(rows)
        after=engine.session_manager.load_subagent_transcript(worker_id)['metadata']['external_agent']
        assert after['codex_thread_id']==before['codex_thread_id'] and after['session_file']==before['session_file']
        assert after['phase']=='idle' and len(model.requests)==4,(after,len(model.requests))
        assert all(body['model']=='owned-codex-model' for body in model.requests)
        assert 'OWNED_EVIDENCE' in str(model.requests[-1]['input'])
    finally:await fixture.asyncTearDown()


if __name__=='__main__':asyncio.run(main())
