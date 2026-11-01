"""Replace the latest human turn without touching workspace files."""
import copy
import uuid
from xuloom.core.storage import history_root, atomic_json


def replace_latest_turn(engine, message_id):
    engine.require_writer()
    from xuloom.desktop.turns import visible_text
    humans = [(index, message) for index, message in enumerate(engine.messages)
              if message.get("role") == "user" and not message.get("_is_meta")
              and message.get("_origin", "human") == "human"
              and not any(block.get("type") == "tool_result" for block in message.get("content", []) if isinstance(block, dict))
              and visible_text(message).strip()]
    if not message_id or not humans or humans[-1][1].get("_uuid") != message_id:
        raise ValueError("Only the latest user message can be edited; refresh the conversation")
    index, _ = humans[-1]
    # Keep an immutable recovery copy in central history before replacing any messages.
    backup = history_root(engine.session_cwd) / "tool-results" / engine.session_id / "revisions" / (str(uuid.uuid4()) + ".json")
    atomic_json(backup, {"messages": engine.messages, "turn_traces": engine.turn_traces,
        "artifacts": engine.artifact_tracker.export_state()})
    messages, traces = engine.messages, engine.turn_traces
    references = copy.deepcopy(engine.artifact_tracker.references)
    engine.artifact_tracker.references.pop(message_id, None)
    engine.messages = copy.deepcopy(messages[:index])
    engine.turn_traces = [trace for trace in traces if trace.get("userMessageId") != message_id]
    engine._sync_message_context()
    try:
        engine.persist_session_state()
        if not engine.session_manager.last_save_ok:
            raise OSError("Could not save the edited conversation")
    except Exception:
        engine.messages, engine.turn_traces = messages, traces
        engine.artifact_tracker.references = references
        engine._sync_message_context()
        raise
