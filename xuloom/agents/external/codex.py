"""Installed Codex App Server sampler over the existing owned MCP/runtime."""
import asyncio
import json
import os
import uuid

from xuloom.core.storage import atomic_text, IOPath, logical_path
from xuloom.core.run_result import PersistenceError
from .host import LocalHost, LIMIT
from .program import environment, _hash
from .runtime import prepare, cleanup, response, ExternalExecutionError


def configuration(home, host, model):
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    rows = ["model = " + quote(model), 'model_provider = "xuloom"', 'project_doc_max_bytes = 0',
        'approval_policy = "never"', 'sandbox_mode = "danger-full-access"', 'web_search = "disabled"',
        'check_for_update_on_startup = false', '[features]',
        *[name + " = false" for name in ("shell_tool", "unified_exec", "multi_agent", "apps", "hooks", "goals", "remote_plugin", "memories")],
        '[model_providers.xuloom]', 'name = "Owned Xuloom connection"', 'base_url = ' + quote(host.url + '/v1'),
        'env_key = "XULOOM_CODEX_TOKEN"', 'wire_api = "responses"', 'supports_websockets = false', 'request_max_retries = 0', 'stream_max_retries = 0',
        '[mcp_servers.xuloom]', 'url = ' + quote(host.url + '/mcp'), 'required = true',
        'http_headers = { Authorization = ' + quote('Bearer ' + host.token) + ' }']
    for name in host.catalog():
        rows.extend(['[mcp_servers.xuloom.tools.' + quote(name) + ']', 'approval_mode = "auto"', 'output_token_limit = 1000000'])
    path = home / 'config/config.toml'; atomic_text(path, '\n'.join(rows) + '\n')
    return path


def owned_rollout(home, value, *, require_file=True):
    if not isinstance(value, str) or not value:
        raise ExternalExecutionError('external_session_missing', 'The saved Codex rollout reference is missing')
    path = logical_path(IOPath(value).resolve())
    if path.suffix != '.jsonl' or not path.is_relative_to(logical_path(IOPath(home / 'config/sessions').resolve())):
        raise ExternalExecutionError('external_history_changed', 'Codex history points outside its owned session directory; it was not read')
    if require_file and not IOPath(path).is_file():
        raise ExternalExecutionError('external_session_missing', 'Codex history is missing; a blank thread was not created')
    return path


async def sample(engine, *, system_prompt, api_messages, turn_count, model_name, max_tokens, event_callback=None, **_):
    from xuloom.core.engine import AbortRequestedError
    prepared = await prepare(engine, system_prompt=system_prompt, api_messages=api_messages, model_name=model_name)
    if prepared.get('cached') is not None: return prepared['cached'], [], False
    state, home, content = prepared['state'], prepared['home'], prepared['content']
    process, tasks, pending, notifications, files = None, [], {}, asyncio.Queue(), []
    try:
        async with LocalHost(engine, state, event_callback=event_callback, turn=turn_count) as host:
            host.maximum = max_tokens
            if state['has_session']:
                saved_file = owned_rollout(home, state.get('session_file'))
                if _hash(IOPath(saved_file)) != state.get('codex_rollout_sha256'):
                    raise ExternalExecutionError('external_history_changed', 'Codex history changed after its confirmed checkpoint; the task was not replayed')
            files.append(configuration(home, host, model_name))
            await host.checkpoint()
            env = environment(home)
            env.update(CODEX_HOME=str(home/'config'), XULOOM_CODEX_TOKEN=host.token,
                HTTP_PROXY=host.url, HTTPS_PROXY=host.url, ALL_PROXY=host.url, NO_PROXY='',
                http_proxy=host.url, https_proxy=host.url, all_proxy=host.url, no_proxy='')
            process = await asyncio.create_subprocess_exec(engine.tool_context['external_backend']['program'], 'app-server',
                cwd=engine.session_cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=LIMIT, **({'creationflags':0x08000000} if os.name=='nt' else {}))
            closing = False
            async def write(row):
                process.stdin.write((json.dumps(row, ensure_ascii=False)+'\n').encode()); await process.stdin.drain()
            async def read_output():
                try:
                    with IOPath(home/(uuid.uuid4().hex+'.jsonl')).open('xb') as transcript:
                        while raw := await process.stdout.readline():
                            transcript.write(raw); row=json.loads(raw)
                            if not isinstance(row,dict): raise ValueError('Invalid app server event')
                            if 'id' in row and ('result' in row or 'error' in row):
                                future=pending.get(row['id'])
                                if future is None: raise ValueError('Unknown app server receipt')
                                if not future.done(): future.set_result(row)
                            elif 'method' in row and 'id' in row:
                                # All model-driven tools belong to the native MCP.
                                await write({'id':row['id'],'error':{'code':-32601,'message':'Unowned operation denied'}})
                                host.fail('protocol_error')
                            else:
                                params=row.get('params') or {}; method=row.get('method')
                                thread=params.get('threadId')
                                if thread is not None and state.get('codex_thread_id') is not None and thread!=state['codex_thread_id']:
                                    raise ValueError('App server changed its owning thread')
                                if method in {'item/agentMessage/delta','item/reasoning/summaryTextDelta','item/reasoning/textDelta'}:
                                    await engine._emit_stream_event(event_callback, {'type':'text_delta' if method=='item/agentMessage/delta' else 'thinking_delta','turn':turn_count,'text':params.get('delta','')})
                                if method=='item/started' and params.get('item',{}).get('type') in {'commandExecution','fileChange','webSearch','imageView','collabToolCall','dynamicToolCall'}:
                                    host.fail('protocol_error')
                                await notifications.put(row)
                        transcript.flush(); os.fsync(transcript.fileno())
                except ValueError: host.fail('protocol_error')
                except OSError as error:
                    host.fail('persistence_error')
                    raise PersistenceError('Codex protocol transcript could not be saved') from error
                finally:
                    if not closing: host.fail('external_process_exited')
                    for future in pending.values():
                        if not future.done(): future.set_exception(ExternalExecutionError('external_process_exited','Owned Codex ended before its receipt'))
            async def read_error():
                try:
                    with IOPath(home/(uuid.uuid4().hex+'.stderr')).open('xb') as transcript:
                        while raw := await process.stderr.read(65536): transcript.write(raw)
                        transcript.flush(); os.fsync(transcript.fileno())
                except OSError as error:
                    host.fail('persistence_error')
                    raise PersistenceError('Codex diagnostic transcript could not be saved') from error
            reader=asyncio.create_task(read_output()); tasks.extend([reader,asyncio.create_task(read_error())])
            async def rpc(method,params):
                identity=uuid.uuid4().hex; future=asyncio.get_running_loop().create_future(); pending[identity]=future
                try:
                    await write({'id':identity,'method':method,'params':params})
                    answer=await asyncio.wait_for(future,30)
                    if 'error' in answer: raise ExternalExecutionError('external_rpc_error','Owned Codex rejected '+method+'; no task was replayed')
                    return answer['result']
                finally: pending.pop(identity,None)
            from xuloom import __version__
            await rpc('initialize',{'clientInfo':{'name':'xuloom','version':__version__},'capabilities':{'experimentalApi':True}})
            await write({'method':'initialized','params':{}})
            params={'model':model_name,'modelProvider':'xuloom','cwd':engine.session_cwd,'baseInstructions':system_prompt,'developerInstructions':'',
                'approvalPolicy':'never','sandbox':'danger-full-access','environments':[]}
            if state['has_session']:
                params['threadId']=state['codex_thread_id']
            initial=await rpc('thread/resume' if state['has_session'] else 'thread/start',params)
            identity=initial['thread']['id']
            if state.get('codex_thread_id') not in {None,identity}: raise ExternalExecutionError('protocol_error','Codex resumed a different thread')
            rollout = owned_rollout(home, initial['thread'].get('path'), require_file=state['has_session'])
            if state['has_session'] and rollout != saved_file:
                raise ExternalExecutionError('external_history_changed', 'Codex resumed a different history file')
            state['codex_thread_id']=identity
            await host.checkpoint()
            inputs=[]
            for block in content:
                if block.get('type')=='text': inputs.append({'type':'text','text':block['text']})
                elif block.get('type')=='image' and block.get('source',{}).get('type')=='base64':
                    source=block['source'];inputs.append({'type':'image','url':'data:'+source['media_type']+';base64,'+source['data']})
                else: raise ValueError('Unsupported Codex input block')
            started=await rpc('turn/start',{'threadId':identity,'input':inputs})
            turn_id=started['turn']['id'];state['codex_turn_id']=turn_id
            await host.checkpoint()
            stopped=asyncio.create_task(engine.abort_event.wait());failed=asyncio.create_task(host.failed.wait());tasks.extend([stopped,failed])
            messages=[]
            while True:
                next_event=asyncio.create_task(notifications.get())
                try:
                    done,_=await asyncio.wait({next_event,stopped,failed},return_when=asyncio.FIRST_COMPLETED)
                    if stopped in done:
                        host.stopping=True
                        try: await rpc('turn/interrupt',{'threadId':identity,'turnId':turn_id})
                        except (ExternalExecutionError, OSError): pass
                        raise AbortRequestedError('Codex was stopped by its owner; its execution was not automatically replayed')
                    if failed in done: raise ExternalExecutionError(host.failure,'Owned Codex transport failed; inspect the saved receipts before continuing')
                    row=await next_event
                finally:
                    if not next_event.done(): next_event.cancel()
                    await asyncio.gather(next_event,return_exceptions=True)
                method=row.get('method');params=row.get('params') or {}
                if method=='item/completed' and params.get('item',{}).get('type')=='agentMessage':
                    item=params['item']
                    if item.get('phase') in {None,'final_answer'}: messages.append(item.get('text',''))
                if method=='turn/completed':
                    turn=params.get('turn',{})
                    if turn.get('id')!=turn_id or turn.get('status')!='completed' or turn.get('error') is not None:
                        raise ExternalExecutionError('external_execution_error','Codex did not return the matching successful turn receipt')
                    break
            closing=True;process.stdin.close()
            await asyncio.wait_for(process.wait(),10);await asyncio.gather(*tasks[:2])
            if process.returncode: raise ExternalExecutionError('external_process_exited','Codex failed while saving its completed conversation')
            if host.failure: raise ExternalExecutionError(host.failure,'Codex ended without an intact owned protocol transcript')
            rollout = owned_rollout(home, str(rollout))
            state.update(phase='idle',has_session=True,result='\n\n'.join(messages),session_file=str(rollout),codex_rollout_sha256=_hash(IOPath(rollout)));state.pop('codex_turn_id',None)
            try: await host.checkpoint()
            except PersistenceError:
                state['phase']='running'
                raise
            return response(state['result'],host.usage),[],False
    except (asyncio.CancelledError,AbortRequestedError,PersistenceError,ExternalExecutionError): raise
    except Exception as error:
        raise ExternalExecutionError('external_execution_error','Owned Codex failed without a confirmed completion; no automatic fallback was started') from error
    finally:
        await cleanup(engine,state,process,tasks,files)
