"""Isolated installed-Python controller process driven by an owned SSH fixture."""
import asyncio
import json
from pathlib import Path
import sys

import xuloom
from xuloom.core.engine import QueryEngine


async def main():
    expected, workspace, action = sys.argv[1:4]
    assert xuloom.__version__ == expected
    package = Path(xuloom.__file__).resolve().parent
    assert 'site-packages' in package.parts, 'Controller must import the installed product'
    engine = QueryEngine(model='controller-placeholder', model_provider='openai',
        api_key='controller-key-never-transfer', api_base_url='http://127.0.0.1:1/parent-must-not-be-used', cwd=workspace)
    engine.permission_manager.approval_policy.set_mode('full')
    try:
        if action == 'spawn':
            reply = await engine.agents('spawn', prompt='Read the execution host evidence', subagent_type='remote-review')
            assert reply['ok'], reply
            row = reply['data']['agents'][0]
        else:
            assert action == 'followup' and engine.load_session(sys.argv[4])
            reply = await engine.agents('followup', agent_id=sys.argv[5], prompt='Continue using the original remote history')
            assert reply['ok'], reply
            row = reply['data']
        assert row['status'] == 'completed', row
        saved = engine.session_manager.load_subagent_transcript(row['agent_id'])
        assert saved['status'] == 'completed'
        print('XULOOM_CONTROLLER_RESULT=' + json.dumps({'session_id': engine.session_id, 'agent_id': row['agent_id'], 'remote_reference': saved['remote_reference'],
            'status': row['status'], 'revision': saved['revision'], 'package': str(package)}), flush=True)
    finally:
        await engine.aclose()
    for name, module in tuple(sys.modules.items()):
        if name == 'xuloom' or name.startswith('xuloom.'):
            path = getattr(module, '__file__', None)
            assert not path or Path(path).resolve().is_relative_to(package), path


asyncio.run(main())
