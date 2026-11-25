"""Token usage, request cost, output budget and the task cost cap."""
from typing import Any, Dict, Optional
from xuloom.core.run_result import finish_run
from xuloom.core.engine_state import EngineState
from xuloom.observability.cost import add_cost, cost_totals


class UsageAccountingMixin(EngineState):
    """Token usage, request cost, output budget and the task cost cap."""

    def _record_token_usage(
        self,
        response,
        loop_state: dict,
        turn: int,
        *,
        system_prompt: str = "",
        api_messages: Optional[list] = None,
        tools_schema: Optional[list] = None,
        thinking_config: Optional[dict] = None,
        replayed: bool = False,
    ):
        usage = getattr(response, "usage", None)
        turn_record: Dict[str, Any] = {
            "turn": turn,
            "model": loop_state.get("active_model"),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0,
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0,
            "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage is not None else 0,
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage is not None else 0,
        }
        source = "api_usage"
        if turn_record["input_tokens"] <= 0 and turn_record["output_tokens"] <= 0:
            turn_record["input_tokens"] = self.compactor.estimate_request_tokens(
                api_messages or [],
                system=system_prompt,
                tools=tools_schema,
                thinking=thinking_config,
            )
            turn_record["output_tokens"] = self.compactor.estimate_content_tokens(
                "assistant",
                getattr(response, "content", []),
            )
            source = self.compactor._local_estimator_mode()
        turn_record["source"] = source

        self.token_usage_history.insert(0, turn_record)
        self.token_usage_history = self.token_usage_history[:50]
        self.tool_context["token_usage_history"] = self.token_usage_history

        for key in [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]:
            self.session_token_usage[key] = int(self.session_token_usage.get(key, 0) or 0) + turn_record[key]

        loop_state["turn_input_tokens"] = turn_record["input_tokens"]
        loop_state["turn_output_tokens"] = turn_record["output_tokens"]
        # Cost: replayed (VCR) responses cost nothing; estimated usage is still priced.
        cost = 0.0 if replayed else self.cost_meter.record(turn_record["model"], turn_record, connection=self.tool_context.get("pricing_connection_id"))
        price = self.cost_meter.price_for(turn_record["model"], connection=self.tool_context.get("pricing_connection_id"))
        currency = price.currency if price else "USD"
        turn_record["cost_amount"] = None if cost is None else round(cost, 8)
        turn_record["cost_currency"] = currency if cost is not None else None
        turn_record["cost_usd"] = turn_record["cost_amount"] if currency == "USD" else None
        if cost is None:
            self.session_cost["unpriced_requests"] = int(self.session_cost.get("unpriced_requests", 0) or 0) + 1
        else:
            add_cost(self.session_cost, cost, currency)
        loop_state["turn_cost_usd"] = turn_record["cost_usd"]
        loop_state["turn_cost_amount"] = turn_record["cost_amount"]
        loop_state["turn_cost_currency"] = turn_record["cost_currency"]

    def _model_span_attributes(self, response, loop_state: dict, replayed: bool) -> dict:
        record = self.token_usage_history[0] if self.token_usage_history else {}
        return {
            "gen_ai.response.model": loop_state.get("active_model"),
            "gen_ai.response.finish_reasons": [str(getattr(response, "stop_reason", "") or "")],
            "gen_ai.usage.input_tokens": record.get("input_tokens"),
            "gen_ai.usage.output_tokens": record.get("output_tokens"),
            "gen_ai.usage.cache_creation_input_tokens": record.get("cache_creation_input_tokens"),
            "gen_ai.usage.cache_read_input_tokens": record.get("cache_read_input_tokens"),
            "xuloom.usage.source": record.get("source"),
            "xuloom.cost_usd": record.get("cost_usd"),
            "xuloom.cost_amount": record.get("cost_amount"),
            "xuloom.cost_currency": record.get("cost_currency"),
            "xuloom.replayed": replayed,
        }

    def _remaining_output_budget(self, loop_state: dict) -> Optional[int]:
        from xuloom.core.output_limits import active_budgets
        from xuloom.features.goals import available_output
        budgets = active_budgets(self.tool_context.get("goal_budget"))
        if budgets:
            return min((available_output(budget) for budget in budgets
                        if budget["budget"]["output_tokens"] is not None), default=None)
        # Lifetime usage is accounting, never an implicit conversation quota.
        limit = loop_state.get("session_output_budget")
        if limit is None:
            return None
        return max(0, int(limit) - int(self.session_token_usage.get("output_tokens", 0) or 0))

    async def _stop_for_cost(self, reason, turn_count, event_callback):
        await self._record_loop_transition(
            "cost_limit", turn=turn_count, event_callback=event_callback,
            task_cost_usd=round(self.cost_meter.task_usd, 6), max_cost_usd=self.cost_meter.limit_usd,
            task_costs=dict(self.cost_meter.task_amounts), max_cost_cny=self.cost_meter.limit_cny)
        self.persist_session_state()
        return finish_run(self, reason, 'cost_limit')

    def _run_span_totals(self) -> dict:
        return {"xuloom.task.costs": dict(self.cost_meter.task_amounts),
                "xuloom.session.costs": cost_totals(self.session_cost),
                "xuloom.task.cost_usd": round(self.cost_meter.task_usd, 6),
                "xuloom.task.unpriced_requests": self.cost_meter.task_unpriced,
                "xuloom.session.cost_usd": round(float(self.session_cost.get("usd", 0.0) or 0.0), 6)}
