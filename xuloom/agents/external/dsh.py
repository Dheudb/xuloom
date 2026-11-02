"""Installed DSH headless sampler over the native owned tool/provider boundary."""
import asyncio
import json
import os
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import atomic_text, IOPath, logical_path
from .host import LocalHost, LIMIT
from .program import environment, _hash
from .runtime import ExternalExecutionError, prepare, cleanup, response
from .dsh_history import identity, history_path, project_result, verify_history


def configuration(home, host, model, task, *, maximum, session=None, instructions="", provider="openai", inputs=None, context_window=200000):
    """Compose only required native services, never the installation's base preset."""
    profile = home / 'config/profiles/xuloom'
    entries = []
    def add(identity, package=None, config=None):
        entry = {'id': identity, 'name': '@deepseek-ai/dsh-' + (package or identity)}
        if config is not None: entry['config'] = config
        entries.append(entry)
    for name in ('llm', 'session', 'session-projection', 'agent', 'tools'):
        add(name)
    add('system-prompt', config={'includeHarnessIdentity': False, 'includeRuntimeContext': False})
    entries.append({'id': 'xuloom-instructions', 'name': logical_path(home / 'instructions.mjs').as_uri()})
    add('agent-default-model', config={'provider': 'xuloom', 'model': model})
    add('agent-loop', config={'agents': []})
    add('session-persistence-jsonl', config={'root': str(home / 'sessions'), 'compression': 'none'})
    add('session-query')
    add('token-meter')
    add('compaction-basic', config={'compactionRetries': 0})
    from xuloom.context.tool_media import MAX_BYTES, MAX_PIXELS
    add('attachment-local', config={'dshHome': str(home / 'config'), 'maxImageBytes': MAX_BYTES,
        'maxImagePixels': MAX_PIXELS, 'maxImageDimension': 16384,
        'normalizedImageMaxPixels': MAX_PIXELS, 'normalizedImageMaxDimension': 16384,
        'normalizedImageMaxBytes': MAX_BYTES})
    add('llm-pi-ai', config={'providers': {'xuloom': {'api': 'openai-completions' if provider == 'openai' else 'anthropic-messages',
        'apiKeyEnv': 'XULOOM_DSH_TOKEN', 'baseURL': host.url + ('/v1' if provider == 'openai' else ''),
        'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
        'models': [{'id': model, 'name': model, 'contextWindow': context_window, 'maxTokens': maximum,
            'input': inputs or ['text', 'image']}]}}})
    add('xuloom-mcp', 'mcp-client', {'serverName': 'xuloom', 'transport': 'streamable-http',
        'url': host.url + '/mcp', 'headers': {'Authorization': 'Bearer ' + host.token},
        'failOnStartupError': True, 'reconnect': {'enabled': False}, 'toolCallTimeoutMs': 86400000})
    runner = {'task': task, 'json': True}
    if session is not None: runner['sessionId'] = session
    add('headless-runner', 'headless', runner)
    files = {home / 'instructions.mjs': "import {readFileSync} from 'node:fs';\nexport const inject=['systemPrompt'];\nexport function apply(ctx){ctx.systemPrompt.section({name:'xuloom:instructions',order:0,complete:true,interpolate:false,text:readFileSync(new URL('./instructions.txt',import.meta.url),'utf8')});}\n",
        profile / 'package.json': json.dumps({'private': True, 'dsh': {'profile': {'bundles': []}}}),
        profile / 'cordis.patch.yml': json.dumps([{'insert': entries}], ensure_ascii=False),
        home / '.env': '', home / 'config/.env': '', home / 'config/cordis.patch.yml': '[]'}
    atomic_text(home / 'instructions.txt', instructions)
    for path, text in files.items(): atomic_text(path, text)
    return list(files)


async def sample(engine, *, system_prompt, api_messages, turn_count, model_name, max_tokens, event_callback=None, **_):
    from xuloom.core.engine import AbortRequestedError
    prepared = await prepare(engine, system_prompt=system_prompt, api_messages=api_messages, model_name=model_name,
        input_types={'text'})
    if prepared.get('cached') is not None: return prepared['cached'], [], False
    state, home, content = prepared['state'], prepared['home'], prepared['content']
    process, tasks, files = None, [], []
    try:
        async with LocalHost(engine, state, event_callback=event_callback, turn=turn_count) as host:
            host.maximum, host.result_projection = max_tokens, project_result
            if state['has_session']:
                saved = history_path(home, state.get('dsh_session_id'), state.get('session_file'))
                if _hash(saved) != state.get('history_sha256') or saved.stat().st_size != state.get('history_offset'):
                    raise ExternalExecutionError('external_history_changed', 'DSH history changed after its confirmed checkpoint; no task was replayed')
            contract = engine._input_contract(model_name)
            files = configuration(home, host, model_name, '\n\n'.join(block['text'] for block in content),
                maximum=max_tokens, session=state.get('dsh_session_id') if state['has_session'] else None,
                instructions=system_prompt, provider=engine.model_provider,
                inputs=['text', 'image'] if contract is None or 'image' in contract else ['text'],
                context_window=int(os.environ.get('XULOOM_CONTEXT_WINDOW', '0')) or 200000)
            await host.checkpoint()
            binding, env = engine.tool_context['external_backend'], environment(home)
            # The backend has no host tools; its cwd is owned too, so DSH's env
            # loader cannot read a project .env. Native MCP uses engine.session_cwd.
            env.update(DSH_HOME=str(home / 'config'), XULOOM_DSH_TOKEN=host.token,
                HTTP_PROXY=host.url, HTTPS_PROXY=host.url, ALL_PROXY=host.url, NO_PROXY='127.0.0.1,localhost,::1')
            process = await asyncio.create_subprocess_exec(binding['runtime'], binding['program'], '--profile', 'xuloom',
                cwd=home, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=LIMIT, **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            seen_session, started, final, ended = False, False, None, None
            async def read_output():
                nonlocal seen_session, started, final, ended
                try:
                    with IOPath(home / (uuid.uuid4().hex + '.jsonl')).open('xb') as transcript:
                        while raw := await process.stdout.readline():
                            transcript.write(raw)
                            row = json.loads(raw)
                            if not isinstance(row, dict) or final is not None: raise ValueError('Invalid DSH event order')
                            kind = row.get('type')
                            if kind == 'session':
                                session = identity(row.get('sessionId'))
                                if seen_session or (state.get('dsh_session_id') and session != state['dsh_session_id']):
                                    raise ValueError('DSH changed its owning session')
                                if logical_path(IOPath(row['cwd']).resolve()) != logical_path(IOPath(home).resolve()):
                                    raise ValueError('DSH changed its owned execution directory')
                                seen_session = True; state['dsh_session_id'] = session
                                await host.checkpoint()
                            elif not seen_session: raise ValueError('DSH emitted content before session identity')
                            elif kind == 'status':
                                if row.get('phase') == 'turn_start':
                                    if started or type(row.get('turn')) is not int or row['turn'] != state.get('dsh_turn', 0) + 1:
                                        raise ValueError('DSH turn identity changed')
                                    started = True; state['dsh_turn'] = row['turn']
                                elif row.get('phase') == 'turn_end':
                                    if not started or ended is not None or row.get('turn') != state.get('dsh_turn'):
                                        raise ValueError('DSH repeated or changed its terminal turn')
                                    ended = row.get('reason')
                            elif kind in {'text', 'thinking'}:
                                if not isinstance(row.get('text'), str): raise ValueError('Invalid DSH content')
                                await engine._emit_stream_event(event_callback, {'type': 'text_delta' if kind == 'text' else 'thinking_delta',
                                    'turn': turn_count, 'text': row['text'], 'truncated': bool(row.get('truncated'))})
                            elif kind == 'final':
                                if ended != {'kind': 'completed'} or not isinstance(row.get('text'), str):
                                    raise ValueError('DSH did not confirm a completed turn')
                                final = row['text']
                            elif kind not in {'tool_call', 'tool_result'}: raise ValueError('Unknown or failed DSH event')
                        transcript.flush(); os.fsync(transcript.fileno())
                except OSError as error:
                    host.fail('persistence_error')
                    raise PersistenceError('DSH protocol transcript could not be saved') from error
                except (ValueError, KeyError, TypeError):
                    host.fail('protocol_error'); raise
            async def read_error():
                try:
                    with IOPath(home / (uuid.uuid4().hex + '.stderr')).open('xb') as transcript:
                        while raw := await process.stderr.read(65536): transcript.write(raw)
                        transcript.flush(); os.fsync(transcript.fileno())
                except OSError as error:
                    host.fail('persistence_error')
                    raise PersistenceError('DSH diagnostics could not be saved') from error
            reader, error_reader = asyncio.create_task(read_output()), asyncio.create_task(read_error())
            waiting, stopped, failed = asyncio.create_task(process.wait()), asyncio.create_task(engine.abort_event.wait()), asyncio.create_task(host.failed.wait())
            tasks.extend([reader, error_reader, waiting, stopped, failed])
            done, _ = await asyncio.wait({reader, waiting, stopped, failed}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done: raise AbortRequestedError('DSH execution was stopped by its owner')
            if failed in done: raise ExternalExecutionError(host.failure, 'DSH stopped without an intact owned execution: ' + host.failure)
            if reader in done: await reader
            await asyncio.wait_for(waiting, 10)
            await asyncio.gather(reader, error_reader)
            if process.returncode or host.failure or final is None:
                raise ExternalExecutionError(host.failure or 'external_process_exited', 'DSH did not finish and save its matching owned result')
            journal = history_path(home, state['dsh_session_id'])
            checkpoint = await asyncio.to_thread(verify_history, home, journal, state, host, final)
            state.update(checkpoint, phase='idle', has_session=True, result=final, session_file=str(logical_path(journal)))
            try: await host.checkpoint()
            except PersistenceError:
                state['phase'] = 'running'; raise
            return response(final, host.usage), [], False
    except (asyncio.CancelledError, AbortRequestedError, PersistenceError, ExternalExecutionError): raise
    except Exception as error:
        raise ExternalExecutionError('external_execution_error', 'DSH did not return a verified owned result; inspect its saved transcript') from error
    finally:
        await cleanup(engine, state, process, tasks, files)
