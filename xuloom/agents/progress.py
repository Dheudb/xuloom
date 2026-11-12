"""Detect repeated read-only observations in a delegated investigation.

No time/request quota: changed evidence and unread pages keep the task running.
The short-lived counters belong to the existing model loop, not a new scheduler.
"""
from xuloom.core.messages import create_user_message
from xuloom.core.run_result import finish_run


EVIDENCE_TOOLS = frozenset({"web_fetch_tool", "web_search_tool", "file_read_tool", "grep_tool", "glob_tool"})


async def observe(engine, results, state, *, turn, event_callback=None):
    definition = engine.tool_context.get("delegation", {}).get("role_definition", {})
    if not engine.agent_depth or not definition.get("read_only"):
        return None
    identities = {row.get("tool_use_id") for row in results}
    rows = [row for row in engine.recent_tool_activity if row.get("tool_use_id") in identities]
    if not rows or any(row.get("tool_name") not in EVIDENCE_TOOLS or not row.get("observation_digest") for row in rows):
        state.pop("worker_stall_count", None)
        return None
    seen = state.setdefault("worker_observations", set())
    evidence = {(row["tool_name"], row["observation_digest"]) for row in rows}
    changed = evidence - seen
    seen.update(evidence)
    state["worker_stall_count"] = 0 if changed else state.get("worker_stall_count", 0) + 1
    count = state["worker_stall_count"]
    if count == 3:
        engine.messages.append(create_user_message(
            "[Worker progress check] The last three read-only tool batches returned only evidence already seen. "
            "Use the evidence already collected to return your findings, sources and unresolved gaps. "
            "Continue investigation only if it yields new information; do not repeatedly read the same results.",
            is_meta=True, origin="worker_stall"))
        engine._sync_message_context()
        if not engine.persist_session_state():
            from xuloom.core.run_result import PersistenceError
            raise PersistenceError("Could not save the worker progress checkpoint")
        await engine._record_loop_transition("worker_stalled", turn=turn, event_callback=event_callback, repeats=count)
    if count >= 5:
        # A blocked receipt is not a successful research result. The parent can
        # inspect the retained transcript and explicitly request further work.
        return finish_run(engine, "Repeated read-only observations produced no new evidence after a progress check. "
            "Investigation is blocked; collected results are retained in the subagent transcript. "
            "Inspect those results and the remaining gaps before deciding whether to continue.", "blocked")
    return None
