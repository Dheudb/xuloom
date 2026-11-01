"""Select a visible reply boundary for the existing atomic session fork."""
import copy


def branch_snapshot(raw, point, source_id):
    from xuloom.desktop.turns import conversation_history
    if not isinstance(point, dict) or point.keys() - {"turn", "text", "userId", "traceId"}:
        raise ValueError("Invalid branch point")
    if type(point.get("turn")) is not int or point["turn"] < 1 or not isinstance(point.get("text"), str):
        raise ValueError("Invalid branch point")
    messages = raw if isinstance(raw, list) else raw["messages"]
    metadata = raw.get("metadata", {}) if isinstance(raw, dict) else {}
    history = conversation_history(messages, metadata.get("turn_traces", []), boundaries=True)
    replies = [item for item in history if item["role"] == "assistant"]
    if point.get("userId") or point.get("traceId"):
        matches = [index for index, item in enumerate(replies)
                   if (not point.get("userId") or item.get("_userId") == point["userId"])
                   and (not point.get("traceId") or item.get("trace", {}).get("id") == point["traceId"])]
    else:
        matches = [point["turn"] - 1] if point["turn"] <= len(replies) else []
    if len(matches) != 1:
        raise ValueError("这条回复已发生变化，请重新打开会话后分支")
    through = matches[0] + 1
    chosen = replies[through - 1]
    if (chosen["text"] != point["text"]
        or point.get("userId") and point["userId"] != chosen.get("_userId")
        or point.get("traceId") and point["traceId"] != chosen.get("trace", {}).get("id")):
        raise ValueError("这条回复已发生变化，请重新打开会话后分支")
    # Carry display history and preferences, never running goals, workers,
    # pending commands, approvals, journals or budgets from the parent.
    traces = [copy.deepcopy(item["trace"]) for item in replies[:through] if item.get("trace")]
    files = {}
    for trace in traces:
        for change in trace.get("files", []):
            files[change["path"]] = copy.deepcopy(change)
    kept = {key: copy.deepcopy(metadata[key]) for key in ("thinking", "collaboration_mode") if key in metadata}
    kept.update(turn_traces=traces, file_changes=list(files.values()),
                forked_from={"session_id": source_id, "through_turn": through, "user_message_id": chosen.get("_userId")})
    references = metadata.get("artifacts", {}).get("references", {})
    ids = {item.get("_userId") for item in replies[:through]}
    kept["artifacts"] = {"references": {key: copy.deepcopy(value) for key, value in references.items() if key in ids}}
    return copy.deepcopy(messages[:chosen["_messageEnd"]]), kept
