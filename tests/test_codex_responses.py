"""Owned Responses/MCP wire contracts; no remote API or account is used."""
import copy
import asyncio
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
if __package__:
    from . import test_external_agents as fixtures
else:
    import test_external_agents as fixtures
from xuloom.agents.external.host import LocalHost, SOURCE_ID


class ResponsesModel:
    def __init__(self, mode="normal", transform=None):
        self.mode, self.requests, self.transform = mode, [], transform
        self.tool, self.arguments = "file_read_tool", {"file_path": "evidence.txt"}
    def __call__(self, handler, body):
        if not handler.path.endswith('/responses'): return False
        self.requests.append(copy.deepcopy(body))
        if any(item.get("type") == "function_call_output" for item in body.get("input", [])):
            item={"id":"msg_final","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"CONTROLLED_CODEX_RESULT","annotations":[]}]}
            response={"id":"resp_final","object":"response","status":"completed","model":body["model"],"output":[item],"usage":{"input_tokens":20,"output_tokens":5,"total_tokens":25}}
            events=[{"type":"response.created","response":{**response,"status":"in_progress","output":[]}},
                {"type":"response.output_item.added","output_index":0,"item":{**item,"content":[],"status":"in_progress"}},
                {"type":"response.content_part.added","output_index":0,"item_id":item["id"],"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}},
                {"type":"response.output_text.delta","output_index":0,"item_id":item["id"],"content_index":0,"delta":"CONTROLLED_CODEX_RESULT"},
                {"type":"response.output_text.done","output_index":0,"item_id":item["id"],"content_index":0,"text":"CONTROLLED_CODEX_RESULT"},
                {"type":"response.content_part.done","output_index":0,"item_id":item["id"],"content_index":0,"part":item["content"][0]},
                {"type":"response.output_item.done","output_index":0,"item":item},{"type":"response.completed","response":response}]
            return self.send(handler,events)
        args = json.dumps(self.arguments)
        item = {"id":"fc_owned","call_id":"call_owned","type":"function_call","name":"exec_command" if self.mode == "builtin" else "mcp__xuloom__file_read_tool", "arguments":args,"status":"completed"}
        if self.mode == 'namespace': item.update(namespace='mcp__xuloom',name=self.tool)
        items = [item]
        if self.mode == "mixed": items.append({**item,"id":"fc_invalid","call_id":"call_invalid","name":"exec_command"})
        response = {"id":"resp_owned","object":"response","status":"completed","model":body["model"],"output":items,"usage":{"input_tokens":12,"output_tokens":5,"total_tokens":17}}
        events = [{"type":"response.created","response":{**response,"status":"in_progress","output":[]}}]
        for index, call in enumerate(items):
            events.extend([
                {"type":"response.output_item.added","output_index":index,"item":{**call,"arguments":"","status":"in_progress"}},
                {"type":"response.function_call_arguments.delta","output_index":index,"item_id":call["id"],"delta":args[:5]},
                {"type":"response.function_call_arguments.delta","output_index":index,"item_id":call["id"],"delta":args[5:]},
                {"type":"response.function_call_arguments.done","output_index":index,"item_id":call["id"],"arguments":args},
                {"type":"response.output_item.done","output_index":index,"item":call},
            ])
        if self.mode != "truncated": events.append({"type":"response.completed","response":response})
        return self.send(handler,events)

    def send(self,handler,events):
        if self.transform: self.transform(events)
        raw = "".join("event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n" for value in events).encode()
        handler.send_response(200); handler.send_header("Content-Type","text/event-stream"); handler.send_header("Content-Length",str(len(raw))); handler.end_headers(); handler.wfile.write(raw)
        return True


@unittest.skipUnless(fixtures.MCP, "optional MCP runtime required")
class ResponsesHostTests(fixtures.ExternalFixture):
    def request(self):
        return {"model":fixtures.MODEL,"stream":True,"store":False,"input":[{"role":"user","content":"Read evidence.txt"}],
            "tools":[{"type":"function","name":name,"parameters":{"type":"object","properties":{}}} for name in
                ("mcp__xuloom__file_read_tool","mcp__xuloom__task_list_tool","exec_command")]+[{"type":"web_search"}]}

    async def call(self, host, values):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers={"Authorization":"Bearer "+host.token}, trust_env=False) as http, streamable_http_client(host.url+"/mcp", http_client=http) as (read,write,_):
            async with ClientSession(read,write) as client:
                await client.initialize(); return await client.call_tool("file_read_tool", values)

    async def scenario(self, mode, transform=None):
        engine = self.engine("openai"); state = {**self.state(engine),"backend":"codex"}
        model = self.parent_handler = ResponsesModel(mode, transform)
        async with LocalHost(engine, state) as host:
            host.maximum = 128
            async with httpx.AsyncClient(trust_env=False) as http:
                result = await http.post(host.url+"/v1/responses",json=self.request(),headers={"Authorization":"Bearer "+host.token})
            events = [json.loads(line[5:]) for line in result.text.splitlines() if line.startswith("data:")]
            yield engine, host, model, events

    async def test_catalog_filters_builtin_and_latent_tools_and_real_mcp_reads_owned_file(self):
        async for engine,host,model,events in self.scenario("normal"):
            self.assertIsNone(host.failure)
            self.assertEqual([tool["name"] for tool in model.requests[0]["tools"]],["mcp__xuloom__file_read_tool"])
            self.assertEqual(model.requests[0]["max_output_tokens"],128)
            item = next(row["item"] for row in events if row["type"] == "response.output_item.done")
            values = json.loads(item["arguments"]); self.assertEqual(values[SOURCE_ID],"call_owned")
            result = await self.call(host, values)
            self.assertFalse(result.isError); self.assertIn("OWNED_EVIDENCE",str(result))
            self.assertEqual(host.usage["output_tokens"],5)

    async def test_eof_before_completion_does_not_release_or_grant_a_call(self):
        async for engine,host,model,events in self.scenario("truncated"):
            self.assertEqual(host.failure,"provider_error"); self.assertEqual(host.calls,{})
            self.assertFalse(any(row["type"] == "response.output_item.done" for row in events))
            self.assertFalse(engine.recent_tool_activity)

    async def test_undeclared_builtin_response_never_reaches_the_cli(self):
        async for engine,host,model,events in self.scenario("builtin"):
            self.assertEqual(host.failure,"provider_error"); self.assertFalse(host.calls)
            self.assertFalse(any(row["type"] == "response.output_item.added" for row in events))
            self.assertFalse(engine.recent_tool_activity)

    async def test_invalid_receipts_and_native_execution_events_cannot_release_tools(self):
        def identity(events): events[2]["item_id"] = "foreign_item"
        def usage(events): events[-1]["response"]["usage"]["output_tokens"] = -1
        def duplicate(events): events[-1]["response"]["output"].append(copy.deepcopy(events[-1]["response"]["output"][0]))
        def early(events): events[0]["response"]["output"] = [{"type":"computer_call","id":"unowned","action":{}}]
        def execution(events): events.insert(1,{"type":"response.web_search_call.in_progress","output_index":0})
        def late(events): events.insert(-1,copy.deepcopy(events[2]))
        for mutation in (identity, usage, duplicate, early, execution, late):
            with self.subTest(mutation=mutation.__name__):
                async for engine,host,model,events in self.scenario("normal",mutation):
                    self.assertEqual(host.failure,"provider_error")
                    self.assertFalse(any(row["type"] == "response.output_item.added" for row in events))
                    self.assertFalse(engine.recent_tool_activity)

    async def test_optional_null_caller_still_uses_the_native_mcp_receipt(self):
        def nullable(events):
            for event in events:
                if event.get('item',{}).get('type')=='function_call': event['item']['caller']=None
        async for engine,host,model,events in self.scenario("normal",nullable):
            self.assertIsNone(host.failure)
            item=next(row['item'] for row in events if row['type']=='response.output_item.done')
            result=await self.call(host,json.loads(item['arguments']))
            self.assertFalse(result.isError);self.assertIn('OWNED_EVIDENCE',str(result))

    async def test_invalid_later_call_cannot_release_earlier_call_or_use_its_mcp_grant(self):
        async for engine,host,model,events in self.scenario("mixed"):
            self.assertEqual(host.failure,"provider_error")
            self.assertFalse(any(row["type"] == "response.output_item.added" for row in events))
            result = await self.call(host,{"file_path":"evidence.txt",SOURCE_ID:"call_owned"})
            self.assertTrue(result.isError); self.assertFalse(engine.recent_tool_activity)

    async def test_absolute_proxy_request_cannot_forward_an_unowned_destination(self):
        engine=self.engine("openai"); state={**self.state(engine),"backend":"codex"}
        async with LocalHost(engine,state) as host:
            host.maximum=128
            async with httpx.AsyncClient(proxy=host.url,trust_env=False) as http:
                denied=await http.post('http://unowned.invalid/v1/responses',json=self.request(),headers={"Authorization":"Bearer "+host.token})
                self.assertEqual(denied.status_code,403);self.assertFalse(self.records)
                self.parent_handler=ResponsesModel()
                result=await http.post(host.url+'/v1/responses',json=self.request(),headers={"Authorization":"Bearer "+host.token})
                self.assertEqual(result.status_code,200);self.assertIn('response.completed',result.text)


@unittest.skipUnless(fixtures.MCP and os.environ.get("XULOOM_TEST_CODEX_PROGRAM"), "set XULOOM_TEST_CODEX_PROGRAM for the installed CLI / loopback contract")
class InstalledCodexTests(fixtures.ExternalFixture):
    def role(self, tools='Read'):
        folder=self.root/'.xuloom/agents';folder.mkdir(parents=True,exist_ok=True)
        program=os.environ['XULOOM_TEST_CODEX_PROGRAM'].replace('\\','/')
        (folder/'codex-reader.md').write_text('---\nname: codex-reader\ndescription: independent evidence\nbackend: codex\nbackend_program: '+program+'\ntools: ['+tools+']\n---\nRead the supplied evidence.\n',encoding='utf-8')

    async def spawn(self, engine, **values):
        result=await engine.agents('spawn',prompt='Read evidence.txt',subagent_type='codex-reader',**values)
        self.assertTrue(result['ok'],result)
        return result['data']['agents'][0]

    async def test_native_queue_followup_keeps_actual_codex_thread_and_results(self):
        self.role()
        self.parent_handler=model=ResponsesModel('namespace')
        engine=self.engine('openai')
        first=await engine.agents('spawn',prompt='Read evidence.txt',subagent_type='codex-reader')
        self.assertTrue(first['ok'],first)
        row=first['data']['agents'][0];self.assertEqual(row['status'],'completed',row)
        saved=engine.session_manager.load_subagent_transcript(row['agent_id'])
        identity=saved['metadata']['external_agent']['codex_thread_id']
        self.assertEqual(saved['metadata']['external_agent']['backend'],'codex')
        second=await engine.agents('followup',agent_id=row['agent_id'],prompt='Summarize the evidence')
        self.assertEqual(second['data']['status'],'completed',second)
        restored=engine.session_manager.load_subagent_transcript(row['agent_id'])
        self.assertEqual(restored['metadata']['external_agent']['codex_thread_id'],identity)
        self.assertEqual(len(model.requests),3)
        self.assertIn('Summarize the evidence',str(model.requests[-1]['input']))
        self.assertIn('OWNED_EVIDENCE',str(model.requests[-1]['input']))

    async def test_parent_restart_resumes_the_same_verified_rollout(self):
        self.role();self.parent_handler=model=ResponsesModel('namespace');engine=self.engine('openai')
        row=await self.spawn(engine);self.assertEqual(row['status'],'completed',row)
        before=engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        identity=engine.session_id;await engine.aclose()
        restored=self.engine('openai');self.assertTrue(restored.load_session(identity))
        result=await restored.agents('followup',agent_id=row['agent_id'],prompt='Continue after parent restart')
        self.assertEqual(result['data']['status'],'completed',result)
        after=restored.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        self.assertEqual(before['codex_thread_id'],after['codex_thread_id'])
        self.assertEqual(before['session_file'],after['session_file']);self.assertNotEqual(before['codex_rollout_sha256'],after['codex_rollout_sha256'])
        self.assertEqual(len(model.requests),3)

    async def test_changed_rollout_is_rejected_before_starting_another_model_request(self):
        from xuloom.core.storage import IOPath
        self.role();self.parent_handler=model=ResponsesModel('namespace');engine=self.engine('openai')
        row=await self.spawn(engine);self.assertEqual(row['status'],'completed',row)
        saved=engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        with IOPath(saved['session_file']).open('ab') as stream: stream.write(b'\n')
        result=await engine.agents('followup',agent_id=row['agent_id'],prompt='Do not replay')
        self.assertEqual(result['data']['receipt']['reason'],'external_history_changed',result)
        self.assertEqual(len(model.requests),2)

    async def test_missing_rollout_does_not_start_a_blank_thread(self):
        from xuloom.core.storage import IOPath
        self.role();self.parent_handler=model=ResponsesModel('namespace');engine=self.engine('openai')
        row=await self.spawn(engine);self.assertEqual(row['status'],'completed',row)
        saved=engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        owned=IOPath(saved['session_file']).resolve();self.assertTrue(owned.is_relative_to(IOPath(self.root).resolve()));owned.unlink()
        result=await engine.agents('followup',agent_id=row['agent_id'],prompt='Do not start blank')
        self.assertEqual(result['data']['receipt']['reason'],'external_session_missing',result)
        self.assertEqual(len(model.requests),2)

    async def test_parent_explicit_deny_cannot_be_overridden_by_codex_approval_auto(self):
        self.role();self.parent_handler=model=ResponsesModel('namespace');engine=self.engine('openai')
        engine.permission_manager.always_deny_tools.add('file_read_tool')
        row=await self.spawn(engine);self.assertEqual(row['status'],'completed',row)
        self.assertNotIn('OWNED_EVIDENCE',str(model.requests))
        recent=engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['tool_activity']['recent']
        self.assertTrue(recent[0]['tool_result']['is_error'],recent)

    async def test_cancel_reaps_owned_process_and_unknown_execution_is_not_replayed(self):
        self.role();model=ResponsesModel('namespace');engine=self.engine('openai');processes=[]
        def hold(handler,body):
            if handler.path.endswith('/responses'): self.release.wait(20)
            return model(handler,body)
        self.parent_handler=hold;create=asyncio.create_subprocess_exec
        async def capture(*args,**kwargs):
            child=await create(*args,**kwargs);processes.append(child);return child
        with patch('asyncio.create_subprocess_exec',capture):
            row=await self.spawn(engine,run_in_background=True)
            async def entered():
                while not self.records: await asyncio.sleep(.02)
            await asyncio.wait_for(entered(),20)
            cancelled=await engine.agents('cancel',agent_id=row['agent_id'])
            self.assertEqual(cancelled['data']['status'],'cancelled',cancelled)
            self.assertFalse(cancelled['data']['external_recovery']['resumable'])
        self.assertTrue(all(child.returncode is not None for child in processes))
        self.release.set()
        result=await engine.agents('followup',agent_id=row['agent_id'],prompt='Do not replay an unknown effect')
        self.assertEqual(result['data']['receipt']['reason'],'external_outcome_unknown',result)
        self.assertEqual(len(self.records),1)

    async def test_failed_final_checkpoint_cannot_advertise_a_resumable_success(self):
        self.role();self.parent_handler=ResponsesModel('namespace');engine=self.engine('openai')
        original=type(engine).persist_session_state;failed=[]
        def persist(worker,*args,**kwargs):
            state=worker.tool_context.get('external_agent') or {}
            if state.get('backend')=='codex' and state.get('phase')=='idle' and state.get('has_session'):
                failed.append(state);return False
            return original(worker,*args,**kwargs)
        with patch.object(type(engine),'persist_session_state',persist):row=await self.spawn(engine)
        self.assertEqual(row['receipt']['reason'],'persistence_error',row)
        self.assertTrue(failed);self.assertTrue(all(state['phase']=='interrupted' for state in failed))
        self.assertFalse(row['external_recovery']['resumable'],row)

    async def test_diagnostic_flush_failure_is_not_reported_as_completed(self):
        from xuloom.core.storage import IOPath
        self.role();self.parent_handler=ResponsesModel('namespace');engine=self.engine('openai')
        original=IOPath.open
        class FailedFlush:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def write(self,raw):return self.stream.write(raw)
            def flush(self):raise OSError('Owned transcript flush fault')
        def opening(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return FailedFlush(stream) if path.suffix=='.stderr' and 'external' in path.parts else stream
        with patch.object(IOPath,'open',opening):row=await self.spawn(engine)
        self.assertEqual(row['receipt']['reason'],'persistence_error',row)
        self.assertFalse(row['external_recovery']['resumable'],row)

    async def test_actual_app_server_reads_through_native_mcp_and_completes_owned_turn(self):
        from xuloom.agents.external.program import environment
        from xuloom.agents.external import responses
        engine=self.engine('openai');state={**self.state(engine),'backend':'codex'}
        home=self.root/'owned-codex';home.mkdir();config=home/'config';config.mkdir()
        self.parent_handler=model=ResponsesModel('namespace')
        captured=[];original=responses.payload
        def recording(host,value): captured.append(copy.deepcopy(value.get('tools',[])));return original(host,value)
        process=None;events=[]
        async with LocalHost(engine,state) as host:
            host.maximum=256
            (config/'config.toml').write_text(f'''model = "{fixtures.MODEL}"
model_provider = "owned"
project_doc_max_bytes = 0
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
check_for_update_on_startup = false
[features]
shell_tool = false
unified_exec = false
multi_agent = false
apps = false
hooks = false
goals = false
remote_plugin = false
memories = false
[model_providers.owned]
name = "Owned test transport"
base_url = "{host.url}/v1"
env_key = "OWNED_CODEX_KEY"
wire_api = "responses"
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
[mcp_servers.xuloom]
url = "{host.url}/mcp"
required = true
http_headers = {{ Authorization = "Bearer {host.token}" }}
[mcp_servers.xuloom.tools.file_read_tool]
approval_mode = "auto"
''',encoding='utf-8')
            env=environment(home);env.update(CODEX_HOME=str(config),OWNED_CODEX_KEY=host.token,HTTP_PROXY=host.url,HTTPS_PROXY=host.url,ALL_PROXY=host.url,NO_PROXY='',http_proxy=host.url,https_proxy=host.url,all_proxy=host.url,no_proxy='')
            process=await asyncio.create_subprocess_exec(os.environ['XULOOM_TEST_CODEX_PROGRAM'],'app-server',cwd=self.root,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=64*1024*1024,**({'creationflags':0x08000000} if os.name=='nt' else {}))
            errors=asyncio.create_task(process.stderr.read());sequence=0
            async def receive():
                raw=await asyncio.wait_for(process.stdout.readline(),30)
                if not raw:raise RuntimeError('Owned Codex exited')
                row=json.loads(raw);events.append(row)
                if 'method' in row and 'id' in row:
                    process.stdin.write((json.dumps({'id':row['id'],'error':{'code':-32601,'message':'Unowned request denied'}})+'\n').encode());await process.stdin.drain()
                return row
            async def rpc(method,params):
                nonlocal sequence
                sequence+=1;identity=sequence
                process.stdin.write((json.dumps({'id':identity,'method':method,'params':params})+'\n').encode());await process.stdin.drain()
                while True:
                    row=await receive()
                    if row.get('id')==identity and ('result' in row or 'error' in row):
                        self.assertNotIn('error',row,row);return row['result']
            try:
                with patch.object(responses,'payload',side_effect=recording):
                    await rpc('initialize',{'clientInfo':{'name':'xuloom-owned-test','version':'0.0.0'},'capabilities':{'experimentalApi':True}})
                    process.stdin.write(b'{"method":"initialized","params":{}}\n');await process.stdin.drain()
                    result=await rpc('thread/start',{'model':fixtures.MODEL,'modelProvider':'owned','cwd':str(self.root),'baseInstructions':'Read the evidence with the available tool.','developerInstructions':'','approvalPolicy':'never','sandbox':'danger-full-access','environments':[]})
                    identity=result['thread']['id']
                    await rpc('turn/start',{'threadId':identity,'input':[{'type':'text','text':'Read evidence.txt'}]})
                    while not any(row.get('method')=='turn/completed' for row in events):await receive()
                completed=next(row['params']['turn'] for row in events if row.get('method')=='turn/completed')
                self.assertEqual(completed['status'],'completed',completed)
                self.assertTrue(any(row.get('method')=='item/agentMessage/delta' and 'CONTROLLED_CODEX_RESULT' in str(row) for row in events))
                self.assertEqual([(t['name'],[x['name'] for x in t.get('tools',[])]) for t in model.requests[0]['tools']],[('mcp__xuloom',['file_read_tool'])])
                self.assertEqual(len(model.requests),2)
                self.assertIn('OWNED_EVIDENCE',str(model.requests[1]['input']))
                self.assertEqual(host.started_calls,{'call_owned'})
            finally:
                process.stdin.close()
                try:await asyncio.wait_for(process.wait(),5)
                except asyncio.TimeoutError:process.kill();await process.wait()
                stderr=await errors
                (Path(__file__).resolve().parents[1]/'desktop/build/codex-mcp-proof.json').write_text(json.dumps({'catalogs':captured,'events':events,'stderr':stderr.decode('utf-8','replace'),'host_failure':host.failure},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == "__main__": unittest.main()
