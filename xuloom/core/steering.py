"""Durable user input at the existing model/tool boundary; no second executor."""
import copy
import uuid
from xuloom.core.messages import create_user_message
from xuloom.core.run_result import PersistenceError


def pending(engine):
    return bool(getattr(engine, 'user_inbox', []))


def restore(items):
    if not isinstance(items, list): raise ValueError('Invalid saved user inbox')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'id','text'} or not isinstance(item['text'],str) or not item['text'].strip():
            raise ValueError('Invalid saved user input')
        uuid.UUID(item['id'])
        if item['id'] in seen: raise ValueError('Duplicate saved user input')
        seen.add(item['id'])
    return copy.deepcopy(items)


def withdraw(engine, identity):
    engine._assert_open();engine.require_writer()
    before = engine.user_inbox
    engine.user_inbox = [item for item in before if item['id'] != identity]
    if len(before) == len(engine.user_inbox): return False
    if not engine.persist_session_state():
        engine.user_inbox = before
        raise PersistenceError('Could not confirm withdrawal; the input may still be pending')
    if not engine.user_inbox: engine.user_input_event.clear()
    notify(engine, {'type':'user_input_withdrawn','id':identity})
    return True


def notify(engine, event):
    callback = engine.tool_context.get('capability_event')
    if callable(callback):
        callback(copy.deepcopy(event))
        return True
    return False


def enqueue(engine, text, request_id=None):
    engine._assert_open()
    if engine.tool_context.get('delegation', {}).get('authority_required'):
        raise ValueError('Send instructions through the original parent controller')
    if not isinstance(text, str) or not text.strip() or len(text) > 200000:
        raise ValueError('Supply a nonempty supplemental instruction (at most 200000 characters)')
    identity = request_id or str(uuid.uuid4())
    try: uuid.UUID(identity)
    except (ValueError, TypeError, AttributeError): raise ValueError('Use a UUID request_id') from None
    text = text.strip()
    for message in engine.messages:
        if message.get('_steering_id') == identity:
            if message['content'] != text: raise ValueError('Input ID belongs to different text')
            return {'id': identity, 'text': text, 'status': 'applied'}
    for item in engine.user_inbox:
        if item['id'] == identity:
            if item['text'] != text: raise ValueError('Input ID belongs to different text')
            return {**item, 'status': 'queued'}
    if engine.active_run_task is None or engine.active_run_task.done() or engine.abort_event.is_set():
        raise ValueError('No running task accepts supplemental input; send a new prompt or resume first')
    engine.require_writer()
    item = {'id': identity, 'text': text}
    engine.user_inbox.append(item)
    if not engine.persist_session_state():
        engine.user_inbox.remove(item)
        engine.abort_event.set()
        raise PersistenceError('Supplemental input was not confirmed saved; existing execution is stopping')
    engine.user_input_event.set()
    receipt = {**item, 'status': 'queued'}
    notify(engine, {'type': 'user_input_queued', 'input': receipt})
    return receipt


async def consume(engine, event_callback=None):
    if not pending(engine) or engine.abort_event.is_set(): return []
    items = copy.deepcopy(engine.user_inbox)
    original = list(engine.messages)
    seen = {m.get('_steering_id') for m in original}
    applied = []
    for item in items:
        if item['id'] in seen: continue
        message = create_user_message(item['text'], msg_uuid=item['id'], origin='human')
        message['_steering_id'] = item['id']
        engine.messages.append(message)
        applied.append(item)
    engine.user_inbox = []
    engine._sync_message_context()
    if not engine.persist_session_state():
        engine.messages = original
        engine.user_inbox = items
        engine._sync_message_context()
        raise PersistenceError('Supplemental input delivery could not be saved; no new model request was started')
    engine.user_input_event.clear()
    event = {'type': 'user_input_applied', 'inputs': applied}
    if applied and not notify(engine, event):
        await engine._emit_stream_event(event_callback, event)
    return applied


def deferred_result(tool_id):
    return {'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': True,
            'content': 'Not executed: new user instructions are pending. Reconsider this call after reading them; completed operations remain valid.'}
