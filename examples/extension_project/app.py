"""A standalone host: custom tool, compatible model endpoint, events, cancel and resume."""
import argparse
import asyncio
import json
import os
from pydantic import BaseModel
from xuloom.core.engine import QueryEngine
from xuloom.tools.base import BaseAgenticTool


class EvidenceInput(BaseModel):
    value: str


class EvidenceTool(BaseAgenticTool):
    name = 'evidence_tool'
    description = 'Return the supplied evidence with a stable marker.'
    input_schema = EvidenceInput
    is_read_only = True
    risk_level = 'low'

    async def execute(self, value):
        return 'EVIDENCE:' + value


async def execute(args):
    async with QueryEngine(model=args.model, model_provider='openai', api_base_url=args.base_url,
        api_key=os.environ.get('EXAMPLE_API_KEY', ''), cwd=os.getcwd(), network_mode='direct') as engine:
        engine.register_tool(EvidenceTool())
        engine.set_event_handler(lambda event: print(json.dumps(event, ensure_ascii=False, default=str), flush=True))
        if args.session and not engine.load_session(args.session):
            raise ValueError('Cannot load the requested session')
        stop_requested = False
        async for event in engine.submit_message('' if args.resume else args.prompt, resume=args.resume, sys_print_callback=lambda _: None):
            print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
            if args.cancel and not stop_requested and event['type'] == 'message_start':
                stop_requested = True
                await engine.request_abort()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--session')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--cancel', action='store_true')
    parser.add_argument('prompt', nargs='?', default='Use evidence_tool to record the assigned evidence, then answer.')
    args = parser.parse_args()
    if args.resume and not args.session: parser.error('--resume requires --session')
    asyncio.run(execute(args))
