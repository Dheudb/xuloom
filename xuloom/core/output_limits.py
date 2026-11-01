"""Shared reservation accounting for goal and delegated model output."""
from contextlib import asynccontextmanager
from xuloom.features.goals import CURRENT_BUDGET, available_output
from xuloom.agents.collaboration import DELEGATION_BUDGET


class ModelBudgetError(RuntimeError):
    """A request was rejected before inference because no output grant remained."""


def active_budgets(goal=None, delegation=None):
    result = []
    for budget in (goal or CURRENT_BUDGET.get(), delegation or DELEGATION_BUDGET.get()):
        if budget is not None and all(budget is not item for item in result):
            result.append(budget)
    return result


def reserve(maximum, budgets):
    amount = min([maximum] + [min(available_output(b), b.get("request_cap", maximum)) for b in budgets])
    if amount <= 0:
        raise ModelBudgetError("Model output-token budget is exhausted or reserved by active workers")
    for budget in budgets:
        budget["output_reserved"] += amount
    return amount


def settle(budgets, amount, reported):
    for budget in budgets:
        budget["output_used"] += reported if reported > 0 else amount
        budget["output_reserved"] -= amount


@asynccontextmanager
async def model_reservation(engine, maximum):
    """One request, including a remote owner's existing grant when applicable."""
    acquire = engine.tool_context.get("acquire_model_budget")
    lease = None
    if acquire is not None:
        lease = await acquire(maximum)
        if not isinstance(lease, dict) or type(lease.get("maximum")) is not int or not 0 < lease["maximum"] <= maximum or not isinstance(lease.get("id"), str):
            raise ValueError("Delegating model budget did not return a valid reservation")
        maximum = lease["maximum"]
    budgets = active_budgets(engine.tool_context.get("goal_budget"))
    reserved = None
    grant = {"maximum": maximum, "reported": 0}
    try:
        if budgets:
            reserved = reserve(maximum, budgets)
            grant["maximum"] = reserved
        yield grant
    finally:
        if reserved is not None:
            settle(budgets, reserved, grant["reported"])
        if lease is not None:
            engine.tool_context["settle_model_budget"](lease, grant["reported"])
