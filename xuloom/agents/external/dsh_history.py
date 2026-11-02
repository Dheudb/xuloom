"""Verify DSH's lossless v4 journal, not its bounded JSON display projection."""
import base64
import hashlib
import json
import re
import os
import uuid

from xuloom.core.run_result import PersistenceError
from xuloom.core.storage import IOPath, logical_path
from .host import LIMIT, PREFIX, SOURCE_ID
from .runtime import ExternalExecutionError


def project_result(name, content, failed):
    """Meet DSH's 8-bit sRGB image contract explicitly, without resizing pixels."""
    from xuloom.context.tool_media import MAX_BYTES, MAX_PIXELS
    import io
    from PIL import Image
    projected = []
    for block in content:
        if block.get('type') != 'image': projected.append(block); continue
        data = base64.b64decode(block['data'], validate=True)
        if len(data) > MAX_BYTES: raise ValueError('DSH image exceeds the native image byte limit')
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS or max(image.size) > 16384:
                raise ValueError('DSH image exceeds the native image dimension limit')
            image.seek(0)
            image = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
            output = io.BytesIO(); image.save(output, format='PNG')
        data = output.getvalue()
        if len(data) > MAX_BYTES: raise ValueError('Lossless DSH image exceeds the native byte limit; no resized image was substituted')
        projected.append({**block, 'data': base64.b64encode(data).decode('ascii'), 'mimeType': 'image/png'})
    return projected, result_digest(name, projected, failed)


def identity(value):
    if not isinstance(value, str) or not value.startswith('session-') or str(uuid.UUID(value[8:])) != value[8:]:
        raise ValueError('Invalid DSH session identity')
    return value


def owned(path, root):
    resolved = logical_path(IOPath(path).resolve())
    if not resolved.is_relative_to(logical_path(IOPath(root).absolute())):
        raise ExternalExecutionError('external_history_changed', 'DSH history points outside its owned directory; it was not read')
    return IOPath(resolved)


def history_path(home, session, saved=None):
    identity(session)
    root = owned(home / 'sessions', home)
    if saved:
        path = owned(saved, root)
        if path.parent.name != session or path.name != 'session.v4.jsonl':
            raise ValueError('Invalid DSH journal reference')
        if not path.is_file(): raise ExternalExecutionError('external_session_missing', 'DSH history is missing; no blank session was created')
        return path
    matches = []
    for bucket in IOPath(root).iterdir():
        bucket = owned(bucket, root)
        if bucket.is_dir():
            path = owned(bucket / session / 'session.v4.jsonl', root)
            if path.is_file(): matches.append(path)
    if len(matches) != 1:
        raise PersistenceError('The exact owned DSH journal could not be located')
    return matches[0]


def result_digest(name, content, failed, *, home=None):
    """Match DSH's newline-coalesced text and immutable image object references."""
    blocks, text = [], []
    def flush():
        if text: blocks.append({'type': 'text', 'text': '\n'.join(text)}); text.clear()
    for block in content:
        if block.get('type') == 'text':
            text.append(block['text'])
        elif block.get('type') == 'image':
            flush()
            if home is None:
                data = base64.b64decode(block['data'], validate=True)
                digest, media = hashlib.sha256(data).hexdigest(), block['mimeType']
            else:
                reference = block['attachment']
                match = re.fullmatch(r'sha256:([a-f0-9]{64})', reference['attachmentId'])
                if not match: raise ValueError('Invalid DSH image reference')
                digest, media = match[1], reference['mediaType']
                from xuloom.context.tool_media import MAX_BYTES, preview_image_bytes
                path = owned(home / 'config/attachments/v1/objects' / digest[:2] / digest, home / 'config/attachments/v1')
                with path.open('rb') as file: data = file.read(MAX_BYTES + 1)
                if len(data) != reference['bytes'] or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('DSH image object does not match its durable reference')
                preview_image_bytes(data)
            blocks.append({'type': 'image', 'sha256': digest, 'mimeType': media})
        else: raise ValueError('Unsupported DSH tool content')
    flush()
    if not blocks: blocks = [{'type': 'text', 'text': '(' + name + ' returned no model-visible content)'}]
    if failed and home is None:
        if any(block['type'] != 'text' for block in blocks): raise ValueError('Unexpected image in failed MCP receipt')
        blocks = [{'type': 'text', 'text': 'Error: ' + '\n'.join(block['text'] for block in blocks)}]
    return hashlib.sha256(json.dumps([blocks, bool(failed)], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_history(home, path, state, host, final):
    calls, results, turn, reason, text, next_seq = {}, {}, None, None, '', 0
    start = state.get('dsh_next_seq', 0)
    digest = hashlib.sha256()
    try:
        with path.open('r+b') as file:
            raw = file.readline(LIMIT + 1)
            header = json.loads(raw)
            if not isinstance(header, dict) or header.get('type') != 'session' or header.get('version') != 4 or header.get('id') != state['dsh_session_id']:
                raise ValueError('DSH journal header changed')
            if logical_path(IOPath(header['cwd']).resolve()) != logical_path(IOPath(home).resolve()) or header.get('parentSession') is not None:
                raise ValueError('DSH journal changed its owner')
            digest.update(raw)
            boundary = state.get('history_offset')
            prefix = digest.hexdigest() if file.tell() == boundary else None
            while raw := file.readline(LIMIT + 1):
                if len(raw) > LIMIT or not raw.endswith(b'\n'): raise ValueError('Incomplete or oversized DSH journal record')
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get('seq') != next_seq or type(row.get('seq')) is not int: raise ValueError('DSH journal sequence changed')
                next_seq += 1; digest.update(raw)
                if file.tell() == boundary: prefix = digest.hexdigest()
                if row['seq'] < start: continue
                data, kind = row.get('data', {}), row['type']
                if not isinstance(data, dict): raise ValueError('DSH journal payload changed')
                if kind == 'turn/start':
                    if turn is not None: raise ValueError('DSH returned more than one owned turn')
                    turn = data['turn']
                elif kind == 'assistant/message' and row.get('surfaceOp') == 'append':
                    message = data['message']
                    joined = ''.join(block['text'] for block in message['content'] if block.get('type') == 'text')
                    if joined: text = joined
                elif kind == 'tool/call':
                    call, name = data['callId'], data['name']
                    args = json.loads(data['arguments'])
                    if call in calls or not name.startswith(PREFIX) or args.pop(SOURCE_ID, None) != call:
                        raise ValueError('DSH journal lost its owned tool identity')
                    calls[call] = (name[len(PREFIX):], args)
                elif kind == 'tool/result' and row.get('surfaceOp') == 'append':
                    message = data['message']; call = message['toolCallId']
                    if call in results or call not in calls: raise ValueError('DSH journal repeated or changed a tool receipt')
                    if call in host.receipts:
                        results[call] = (calls[call][0], result_digest(calls[call][0], message['content'], message.get('isError', False), home=home))
                    elif message.get('isError') is True:
                        results[call] = None
                    else: raise ValueError('DSH reported an unexecuted tool as successful')
                elif kind == 'turn/end':
                    if reason is not None or data['turn'] != turn: raise ValueError('DSH turn completion changed')
                    reason = data['reason']
            size = file.tell()
            if boundary and prefix != state.get('history_sha256'): raise ValueError('DSH journal changed its confirmed prefix')
            if reason != {'kind': 'completed'} or text != final or turn != state['dsh_turn']:
                raise ValueError('DSH journal and terminal completion disagree')
            if calls != host.calls or set(results) != set(calls) or host.started_calls != set(host.receipts) or host.engine.in_progress_tools:
                raise ValueError('DSH and native tool execution could not be reconciled')
            for call, result in results.items():
                if call in host.receipts:
                    if result != host.receipts[call]:
                        raise ValueError('DSH history did not acknowledge the complete native tool result')
            os.fsync(file.fileno())
        return {'history_sha256': digest.hexdigest(), 'history_offset': size, 'entry_cursor': str(next_seq - 1), 'dsh_next_seq': next_seq}
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise PersistenceError('DSH result/history verification failed; the execution was not made resumable') from error
