"""Maintenance adds CLI/RPC only: no model tools or prompt text."""
import asyncio
from pathlib import Path


def register(api):
    api.add_cli_command('maintenance', configure, dispatch, help='Back up, restore and diagnose local data (offline)')
    for action in ('backup', 'inspect', 'diagnose', 'importSettings'):
        api.add_rpc_method('maintenance.' + action, handler(action), settings=action in {'backup', 'importSettings'})


def configure(parser):
    sub = parser.add_subparsers(dest='maintenance_action', required=True)
    for name in ('backup', 'diagnose'):
        item = sub.add_parser(name)
        item.add_argument('--output', required=True)
    sub.add_parser('inspect').add_argument('archive')
    sub.add_parser('import-settings')
    item = sub.add_parser('restore')
    item.add_argument('archive')
    item.add_argument('--confirm', action='store_true')


def run(action, params):
    from xuloom.core.storage import data_directory
    from xuloom.maintenance.archive import backup, diagnostic, inspect_backup, restore
    if action in {'importSettings', 'import-settings'}:
        from xuloom.maintenance.archive import import_settings
        return import_settings(data_directory())
    if action == 'inspect':
        value = inspect_backup(params['archive'])
        return {'version': value['version'], 'files': len(value['files']), 'dataRoot': value['dataRoot']}
    if action == 'restore':
        return restore(data_directory(), params['archive'], confirmed=params.get('confirm') is True)
    if action == 'diagnose':
        return diagnostic(data_directory(), Path(params['output']))
    from xuloom.core.config import load_config
    from xuloom.observability.cost import load_pricing_settings
    prices, cap = load_pricing_settings()
    from dataclasses import asdict
    return backup(data_directory(), params['output'], load_config(),
                  {'models': {k: asdict(v) for k, v in prices.items()}, 'max_cost_usd': cap})


def handler(action):
    async def invoke(params, context):
        if action in {'backup', 'importSettings'}:
            context.require_idle()
        return await asyncio.to_thread(run, action, params)
    return invoke


async def dispatch(args, emit):
    value = await asyncio.to_thread(run, args.maintenance_action, vars(args))
    return {'ok': True, 'text': str(value), **value}
