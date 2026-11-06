"""Read-only dialogue projections over existing native receipts and CLI journals.

No SessionManager is used: inspecting a checkpoint must never rebase a writer.
Paging is a content-bound snapshot, not an execution or recovery cursor.
"""
import base64
import hashlib
import json
import re
import uuid

from xuloom.core.storage import IOPath, history_root, artifact_directory, logical_path
from xuloom.features.chat_links import _read

IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
CHUNK = 8192
MAX_LINE = 32 * 1024 * 1024
MAX_HISTORY = 256 * 1024 * 1024


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("Invalid saved conversation or agent identity")
    return value


def record(cwd, session_id, agent_id=None):
    root = history_root(cwd)
    session_id = identity(session_id)
    raw = saved(root / 'sessions' / (session_id + '.json'), root)
    if isinstance(raw, list):
        raw = {'messages': raw, 'metadata': {}}
    if not isinstance(raw, dict) or not isinstance(raw.get('messages'), list):
        raise ValueError("Saved conversation is missing or unreadable")
    if not isinstance(raw.get('metadata', {}), dict):
        raise ValueError("Saved conversation metadata is invalid")
    if agent_id is None:
        return raw, None
    agent_id = identity(agent_id)
    workers = raw.get('metadata', {}).get('subagents', [])
    worker = next((r for r in workers if isinstance(r, dict) and r.get('agent_id') == agent_id), None) if isinstance(workers, list) else None
    if worker is None:
        raise ValueError("Agent is not registered to this conversation")
    child = saved(root / 'sessions' / 'subagents' / (agent_id + '.json'), root)
    metadata = child.get('metadata', {}) if isinstance(child, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    agent = metadata.get('agent', {})
    agent = agent if isinstance(agent, dict) else {}
    delegation = metadata.get('delegation', {})
    delegation = delegation if isinstance(delegation, dict) else {}
    if child is None and worker.get('status') == 'queued':
        child = {'messages': [], 'metadata': {}, 'status': 'queued'}
    elif (not isinstance(child, dict) or child.get('agent_id', agent.get('agent_id')) != agent_id
          or agent.get('agent_id', agent_id) != agent_id
          or child.get('session_id', delegation.get('owner_session_id')) != session_id
          or not isinstance(child.get('messages'), list)
          or not isinstance(child.get('metadata'), dict)
          or not isinstance(child['metadata'].get('delegation'), dict)
          or child.get('metadata', {}).get('delegation', {}).get('owner_session_id') != session_id):
        raise ValueError("Agent receipt is missing or belongs to another conversation")
    return child, worker


def owned(path, root):
    """Reject symlinks/junctions at every component, including the owned root."""
    path, root = IOPath(logical_path(path)), IOPath(logical_path(root))
    logical_path(path).relative_to(logical_path(root))
    probe = path
    while True:
        if probe.is_symlink() or getattr(probe, 'is_junction', lambda: False)():
            raise ValueError("Linked history paths are not readable")
        if probe == root:
            break
        probe = probe.parent
    logical_path(path.resolve()).relative_to(logical_path(root.resolve()))
    return path


def saved(path, root):
    path = owned(path, root.parent)
    if path.exists() and path.stat().st_size > MAX_HISTORY:
        raise ValueError("Saved history exceeds 256 MiB; the original record was preserved")
    return _read(path, root.parent)


def source(cwd, session_id, raw):
    state = raw.get('metadata', {}).get('external_agent')
    if state is None:
        return 'native', None, None
    if not isinstance(state, dict):
        raise ValueError("Invalid external history checkpoint")
    backend = state.get('backend')
    sid = state.get('session_id')
    if backend not in {'claude', 'pi', 'codex', 'dsh'} or str(uuid.UUID(sid)) != sid:
        raise ValueError("Invalid external history identity")
    owner = raw.get('metadata', {}).get('delegation', {}).get('owner_session_id', session_id)
    root = history_root(cwd)
    home = owned(artifact_directory(cwd) / identity(owner) / 'external' / sid, root)
    if backend == 'claude':
        projects = owned(home / 'config' / 'projects', root)
        paths = [owned(p, root) for p in projects.glob('*/' + sid + '.jsonl')]
        if len(paths) != 1:
            raise ValueError("Claude's owned conversation journal is missing or ambiguous")
        path = paths[0]
    else:
        folder = home / 'config' / 'sessions' if backend == 'codex' else home / 'sessions'
        path = owned(state.get('session_file') or folder / 'missing', root)
        logical_path(path).relative_to(logical_path(folder))
    if path.suffix != '.jsonl' or not path.is_file():
        raise ValueError("External conversation journal is unavailable")
    return backend, path, state


def lines(path):
    with path.open('rb') as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                raise ValueError("History record exceeds the safe display size; the original journal was preserved")
            if not line.endswith(b'\n'):
                return  # A live writer may not have committed its last JSONL record yet.
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Invalid conversation journal record")
                yield row


def messages(backend, path, raw, state):
    if backend == 'native':
        yield from raw['messages']
        return
    first = True
    for row in lines(path):
        kind = row.get('type')
        if first:
            first = False
            if backend in {'pi', 'dsh'}:
                expected = state['session_id'] if backend == 'pi' else state.get('dsh_session_id')
                if kind != 'session' or row.get('id') != expected or row.get('version') != (3 if backend == 'pi' else 4):
                    raise ValueError("External journal header does not match this agent")
            elif backend == 'codex' and (kind != 'session_meta' or row.get('payload', {}).get('id') != state.get('codex_thread_id')):
                raise ValueError("Codex journal belongs to another thread")
            elif backend == 'claude' and row.get('sessionId') != state['session_id']:
                raise ValueError("Claude journal is missing its owned session identity")
        if backend == 'claude':
            if row.get('sessionId') not in (None, state['session_id']):
                raise ValueError("Claude journal belongs to another session")
            if kind in {'user', 'assistant'}:
                yield row.get('message', {})
        elif backend == 'pi' and kind == 'message':
            yield row.get('message', {})
        elif backend == 'dsh' and kind in {'user/message', 'assistant/message', 'tool/result'}:
            data = row.get('data', {})
            # tool/call and stream deltas duplicate the committed message blocks.
            yield data.get('message', data)
        elif backend == 'codex' and kind == 'response_item':
            value = row.get('payload', {})
            if value.get('type') == 'message':
                yield value
            elif value.get('type') in {'function_call', 'custom_tool_call'}:
                yield {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': value.get('call_id'),
                    'name': value.get('name'), 'input': value.get('arguments', value.get('input', ''))}]}
            elif value.get('type') in {'function_call_output', 'custom_tool_call_output'}:
                yield {'role': 'tool', 'toolCallId': value.get('call_id'), 'content': value.get('output', '')}
            elif value.get('type') == 'reasoning':
                yield {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '\n'.join(
                    part.get('text', '') for part in value.get('summary', []) if isinstance(part, dict))}]}
    if first:
        raise ValueError("External journal has no complete session header")


def events(backend, path, raw, state):
    for message in messages(backend, path, raw, state):
        if not isinstance(message, dict) or message.get('role') not in {'user', 'assistant', 'tool', 'toolResult'}:
            continue  # Configuration, transport events and hidden system instructions are not dialogue.
        role = message['role']
        tool = role in {'tool', 'toolResult'}
        content = message.get('content', [])
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            content = [{'type': 'unsupported'}]
        for block in content:
            if not isinstance(block, dict):
                yield {'role': role, 'kind': 'notice', 'text': '无法展示的内容块；原始记录保留。'}
                continue
            kind = block.get('type')
            base = {'role': role}
            if tool:
                base.update(kind='tool_result', callId=message.get('toolCallId'), error=bool(message.get('isError')))
            if kind in {'text', 'input_text', 'output_text'}:
                yield {**base, 'kind': 'tool_result' if tool else 'text', 'text': str(block.get('text', ''))}
            elif kind in {'thinking', 'reasoning'}:
                yield {**base, 'kind': 'thinking', 'text': str(block.get('thinking', block.get('text', '')))}
            elif kind in {'tool_use', 'toolCall', 'tool-call'}:
                value = block.get('input', block.get('arguments', {}))
                yield {**base, 'kind': 'tool_call', 'callId': block.get('id'), 'name': block.get('name'),
                    'text': value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)}
            elif kind == 'tool_result':
                yield from events('native', None, {'messages': [{'role': 'tool', 'toolCallId': block.get('tool_use_id'),
                    'isError': block.get('is_error'), 'content': block.get('content', '')}]}, None)
            elif kind in {'image', 'image_url', 'input_image'}:
                yield {**base, 'kind': 'image', 'text': '', '_image': block}
            elif kind == 'redacted_thinking':
                yield {**base, 'kind': 'notice', 'text': '模型未公开此段推理内容。'}
            else:
                yield {**base, 'kind': 'notice', 'text': '暂不支持展示的内容类型：' + str(kind)[:80] + '；原始记录保留。'}


def snapshot(cwd, session_id, agent_id=None):
    raw, worker = record(cwd, session_id, agent_id)
    if worker and worker.get('executor', 'local') != 'local':
        raise ValueError("Use the saved SSH execution host to inspect this agent")
    backend, path, state = source(cwd, session_id, raw)
    digest = hashlib.sha256()
    if path:
        size = path.stat().st_size
        if size > MAX_HISTORY:
            raise ValueError("History exceeds 256 MiB; use the preserved native journal directly")
        before = path.stat()
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(data)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("History is being appended; refresh to read a consistent snapshot")
    else:
        digest.update(json.dumps(raw['messages'], ensure_ascii=False, sort_keys=True).encode())
    # Bind a cursor to the exact parent, worker and source. Never accept a path from the UI.
    digest.update(json.dumps([str(history_root(cwd)), session_id, agent_id, backend]).encode())
    return raw, backend, path, state, digest.hexdigest()


def read(cwd, session_id, agent_id=None, *, cursor=None, limit=40):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("History page limit must be between 1 and 100")
    raw, backend, path, state, version = snapshot(cwd, session_id, agent_id)
    partial = False
    if path and path.stat().st_size:
        with path.open('rb') as stream:
            stream.seek(-1, 2)
            partial = stream.read(1) != b'\n'
        if partial and state.get('phase') == 'idle':
            raise ValueError("The saved journal ends in an incomplete record; existing history was preserved")
    start = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
            start = value['index']
            if value['version'] != version or type(start) is not int or not 0 <= start <= 10_000_000:
                raise ValueError()
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ValueError("History changed or the page cursor is invalid; refresh without continuing execution") from None
    result, position, more, chars = [], 0, False, 0
    for event_index, event in enumerate(events(backend, path, raw, state)):
        text = event.get('text', '')
        for offset in range(0, max(1, len(text)), CHUNK):
            if position >= start:
                if len(result) >= limit or chars >= 65536:
                    more = True
                    break
                chunk = {key: value for key, value in event.items() if key != '_image'}
                chunk.update(id=str(event_index) + ':' + str(offset), text=text[offset:offset + CHUNK], continued=offset > 0,
                    continues=offset + CHUNK < len(text))
                if event['kind'] == 'image':
                    chunk['imageIndex'] = event_index
                result.append(chunk)
                chars += len(chunk['text'])
            position += 1
        if more:
            break
    if path:
        # A live append must not turn the page into an apparently consistent final snapshot.
        check = snapshot(cwd, session_id, agent_id)[4]
        if check != version:
            raise ValueError("History changed while reading; refresh the snapshot")
    if start > position:
        raise ValueError("History cursor is past the saved conversation")
    next_cursor = base64.urlsafe_b64encode(json.dumps({'version': version, 'index': position}).encode()).decode() if more else None
    return {'version': version, 'backend': backend, 'sessionId': session_id, 'agentId': agent_id,
        'entries': result, 'nextCursor': next_cursor, 'readOnly': True,
        'live': bool(state and state.get('phase') == 'running'), 'partial': partial,
        'notice': '按原生记录顺序展示已保存的对话与工具内容；系统配置及传输元数据不展示。未公开的推理无法还原。'
            + (' 最后一条记录尚未写完，刷新后可继续查看。' if partial else '')}


def image(cwd, session_id, agent_id, index, version):
    from xuloom.context.tool_media import preview_image_bytes, MAX_BYTES
    if type(index) is not int or not 0 <= index <= 10_000_000:
        raise ValueError("Invalid history image index")
    raw, backend, path, state, current = snapshot(cwd, session_id, agent_id)
    if not isinstance(version, str) or current != version:
        raise ValueError("History image snapshot changed; refresh first")
    for position, event in enumerate(events(backend, path, raw, state)):
        if position != index:
            continue
        block = event.get('_image', {})
        if backend == 'dsh' and isinstance(block.get('attachment'), dict):
            reference = block['attachment']
            match = re.fullmatch(r'sha256:([a-f0-9]{64})', reference.get('attachmentId', ''))
            if not match:
                raise ValueError("Invalid immutable image reference")
            digest = match[1]
            owner = raw.get('metadata', {}).get('delegation', {}).get('owner_session_id', session_id)
            home = artifact_directory(cwd) / identity(owner) / 'external' / state['session_id']
            file = owned(home / 'config/attachments/v1/objects' / digest[:2] / digest, history_root(cwd))
            with file.open('rb') as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) != reference.get('bytes') or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Saved image bytes do not match the journal's immutable reference")
            return preview_image_bytes(data)
        source = block.get('source', {})
        if not isinstance(source, dict): source = {}
        encoded = source.get('data') if source.get('type') == 'base64' else block.get('data')
        url = block.get('image_url', block.get('url'))
        if isinstance(url, dict):
            url = url.get('url')
        if isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url:
            encoded = url.split(';base64,', 1)[1]
        if not isinstance(encoded, str) or len(encoded) > 28 * 1024 * 1024:
            raise ValueError("This image has no supported embedded preview; no external address was downloaded")
        return preview_image_bytes(base64.b64decode(encoded, validate=True))
    raise ValueError("Saved history image does not exist")


def command_params(argument):
    parts = argument.split()
    if len(parts) > 2:
        raise ValueError('Use /history [agent-id|-] [page-cursor]')
    return {'agentId': identity(parts[0]) if parts and parts[0] != '-' else None,
        'cursor': parts[1] if len(parts) == 2 else None}


async def inspect(cwd, session_id, *, agent_id=None, cursor=None, limit=40, image_index=None, version=None):
    """Same read path for local App/CLI and a known, frozen SSH worker target."""
    import asyncio
    raw, worker = await asyncio.to_thread(record, cwd, session_id, agent_id)
    if worker and worker.get('executor', 'local') != 'local':
        from xuloom.agents.executors import profile
        from xuloom.remote import worker_protocol as wire
        from xuloom.remote.ssh import SSHClient
        spec = worker.get('dispatch', {}).get('remote', {})
        owner = spec.get('open', {}).get('owner', {})
        if (not isinstance(spec.get('profile'), dict) or owner.get('parent_session_id') != session_id
                or owner.get('agent_id') != agent_id):
            raise ValueError("Remote history owner does not match the parent")
        remote_session = wire.session_id(owner)
        target = profile(worker['executor'], frozen=spec.get('profile'))
        client = SSHClient(target, lambda _: None)
        try:
            await client.start()
            await client.request('initialize', {'sessionId': remote_session})
            params = {'cursor': cursor, 'limit': limit, 'imageIndex': image_index, 'version': version}
            return await client.request('session.transcriptImage' if image_index is not None else 'session.transcript', params)
        finally:
            await client.aclose()
    if image_index is not None:
        return await asyncio.to_thread(image, cwd, session_id, agent_id, image_index, version)
    return await asyncio.to_thread(read, cwd, session_id, agent_id, cursor=cursor, limit=limit)
