"""Output admission over the existing durable permit ledger, including offline grants.

Unknown usage consumes its reservation. Host disconnection never refunds it.
This bounds admitted output, not provider billing or external side effects.
"""
import json
import math
import time


class Exhausted(ValueError):
    pass


def normalize(value):
    if value is None: return {}
    if not isinstance(value, dict) or set(value) - {'output_tokens', 'seconds'}:
        raise ValueError('Scaling budget accepts output_tokens and seconds only')
    result = {}
    for key, amount in value.items():
        if amount is None: continue
        if type(amount) is not int or not 1 <= amount <= 10**12:
            raise ValueError('Scaling budget limits must be positive integers')
        result[key] = amount
    return result


def usage(db, run):
    rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='permit' AND parent=?", (run['id'],))]
    return sum(r.get('output_charge', r.get('output_reserved', 0)) for r in rows)


def grant(db, run, maximum):
    if type(maximum) is not int or maximum < 1: raise ValueError('Invalid output reservation')
    budget = run.get('budget') or {}
    if budget.get('deadline') is not None and time.time() >= budget['deadline']: return 0
    cap = budget.get('output_tokens')
    return maximum if cap is None else max(0, min(maximum, cap - usage(db, run)))


def view(store, run):
    with store.connection() as db:
        charged = usage(db, run)
        rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM records WHERE kind='permit' AND parent=?", (run['id'],))]
    return {**(run.get('budget') or {}), 'committed_output_tokens': charged,
            'reported_output_tokens': sum(r.get('reported_output', 0) for r in rows),
            'unknown_usage_requests': sum(bool(r.get('unknown_usage')) for r in rows),
            'active_requests': sum(r['status'] == 'granted' and not r.get('allocation') for r in rows),
            'offline_reserved_output': sum(r.get('output_reserved', 0) for r in rows if r.get('budget_allocation'))}


def settle(store, run, request, quota):
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        raw = db.execute("SELECT data FROM records WHERE kind='permit' AND id=?", (request['id'],)).fetchone()
        old = json.loads(raw[0]) if raw else {'id': request['id'], 'run_id': run['id'], 'quota': quota}
        if old['run_id'] != run['id'] or old['quota'] != quota: raise ValueError('Foreign output reservation')
        if 'output_charge' not in old and old.get('output_reserved') is not None:
            reported = request.get('reported')
            known = type(reported) is int and reported > 0
            old.update(output_charge=max(0, reported) if known else old['output_reserved'],
                       reported_output=reported if known else 0, unknown_usage=not known)
        return store.put_db(db, 'permit', {**old, 'status': 'released'})


def allocate_child(controller, run, task):
    """Reserve one immutable child envelope before dispatch; retries cannot mint credit."""
    if not run.get('budget'): return {}
    key = 'budget-' + task['id']
    with controller.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        raw = db.execute("SELECT data FROM records WHERE kind='permit' AND id=?", (key,)).fetchone()
        if raw: return json.loads(raw[0])['child_budget']
        cap = run['budget'].get('output_tokens')
        maximum = math.ceil(cap * .8 * max(1, task['count']) / max(1, run['target'])) if cap is not None else 1
        amount = grant(db, run, maximum)
        if amount <= 0: raise Exhausted('Scaling output budget or admission deadline is exhausted')
        child = {k:v for k,v in run['budget'].items() if k == 'deadline'}
        if cap is not None: child['output_tokens'] = amount
        controller.store.put_db(db, 'permit', {'id':key, 'run_id':run['id'], 'status':'reserved',
            'budget_allocation':True, 'output_reserved':amount if cap is not None else 0, 'child_budget':child})
        return child
