"""Persist the visible execution transcript separately from model input."""
import copy
import re
import time
import uuid

from xuloom.desktop.activity import timestamp, transcript_activity
from xuloom.protocols.dsml import marker_position
from xuloom.features.file_changes import describe_change, legacy_changes
import os


class TurnRecorder:
    def __init__(self, previous=None):
        self.clock = time.monotonic()
        self.baselines = {}
        self.record = {"id": str(uuid.uuid4()), "startedAt": int(time.time() * 1000),
                       "status": "running", "steps": [], "pendingText": "", "answer": "", "files": []}
        self.elapsed_before_resume = 0
        self.stream_offset = 0
        self.answer_prefix = ""
        self.first_resumed_message = bool(previous)
        if previous:
            self.record = copy.deepcopy(previous)
            self.elapsed_before_resume = previous.get("elapsedMs", 0)
            self.answer_prefix = previous.get("answer") or previous.get("pendingText", "")
            self.stream_offset = max((item.get("turn", 0) for item in previous.get("modelStreams", [])), default=0)
            self.record.update(status="running", answer="", pendingText=self.answer_prefix,
                               resumedAt=int(time.time() * 1000), elapsedBeforeResumeMs=self.elapsed_before_resume)
            self.record.pop("finishedAt", None)
            self.record.pop("elapsedMs", None)
            self.record.pop("runtimeStatus", None)
            for step in self.record["steps"]:
                if step.get("notice", {}).get("status") == "retrying":
                    step["notice"]["status"] = "interrupted"

    def progress(self, status):
        """Presentation only: retire a retry when the next request makes progress."""
        if self.record.get("runtimeStatus") == "retrying":
            for step in self.record["steps"]:
                notice = step.get("notice", {})
                if notice.get("status") == "retrying":
                    notice["status"] = status if status in {"error", "cancelled"} else "completed"
        self.record["runtimeStatus"] = status

    def flush(self):
        if self.record["pendingText"].strip():
            self.record["steps"].append({"kind": "text", "text": self.record["pendingText"]})
        self.record["pendingText"] = ""
        self.answer_prefix = ""

    def accept(self, event):
        kind = event.get("type")
        record = self.record
        if kind == "collaboration_decision":
            if event.get("agent_id"): return False
            identity = "collaboration:" + event["id"]
            record["runtimeStatus"] = "planning_collaboration" if event["status"] == "running" else "waiting"
            if event["status"] == "running": return True
            if event.get("decision") == "answer": return True
            title = (f"协作决策 · 计划派发 {event['worker_count']} 项独立分工" if event.get("decision") == "delegate" else "协作决策 · 主代理独立处理")
            notice = {"id": identity, "reason": kind, "status": "completed", "title": title,
                "message": event.get("reason", ""), "detail": event.get("main_task", "")}
            record["steps"] = [s for s in record["steps"] if s.get("notice", {}).get("id") != identity] + [{"kind": "notice", "notice": notice}]
            return True
        if kind == "context_compaction":
            if event.get("agent_id"): return False
            self.flush()
            step = next((step for step in record["steps"] if step.get("kind") == "compaction" and step["compaction"].get("id") == event["id"]), None)
            if step is None:
                step = {"kind": "compaction", "compaction": {"id": event["id"]}}
                record["steps"].append(step)
            step["compaction"].update({key: event[key] for key in ("status", "before", "after", "elapsedMs") if key in event})
            return True
        if kind == "stream_status":
            if event.get("agent_id"): return False
            streams = record.setdefault("modelStreams", [])
            turn = event["turn"] + self.stream_offset
            entry = next((item for item in streams if item.get("turn") == turn), None)
            if entry is None:
                entry = {"turn": turn}
                streams.append(entry)
            entry.update(event["metrics"])
            status = event["metrics"].get("status")
            if status in {"reasoning", "receiving", "finishing", "completed", "error", "cancelled"}:
                self.progress(status)
            elif record.get("runtimeStatus") != "retrying":
                record["runtimeStatus"] = status
            return True
        if kind == "model_status":
            if event.get("agent_id"): return False
            status = event["status"]
            if status in {"reasoning", "receiving"}:
                self.progress(status)
            elif record.get("runtimeStatus") != "retrying":
                record["runtimeStatus"] = status
            return True
        if kind == "file_changed":
            change = event["change"]
            identity = os.path.normcase(change["path"])
            if identity not in self.baselines: self.baselines[identity] = event.get("before")
            previous = next((item for item in record["files"] if os.path.normcase(item["path"]) == identity), None)
            change = describe_change(change["path"], self.baselines[identity], event.get("after"), (previous or change)["operation"])
            if event["change"].get("revisionId"):
                change["revisionId"] = event["change"]["revisionId"]
            record["files"] = [item for item in record["files"] if os.path.normcase(item["path"]) != identity] + [change]
            return True
        if kind == "text_delta":
            if event.get("channel") == "commentary": record["outputChannel"] = "commentary"
            if event.get("text"): self.progress("receiving")
            record["pendingText"] += event.get("text", "")
            return False  # A trace containing this delta plus the delta event would duplicate text.
        if kind in {"message_start", "turn_started"}:
            if self.first_resumed_message:
                if kind == "message_start": self.first_resumed_message = False
            else:
                self.flush()
            record["outputChannel"] = event.get("channel", "answer")
        elif kind == "message_stop":
            if event.get("stop_reason") == "tool_use":
                self.flush()
        elif kind in {"tool_scheduled", "tool_started", "tool_result"}:
            if event.get("agent_id"):
                return False  # Child details belong to their own inspector cards.
            if kind != "tool_result": self.progress("tools")
            self.flush()
            identity = "main:" + str(event.get("tool_use_id") or event.get("id"))
            step = next((s for s in record["steps"] if s.get("tool", {}).get("id") == identity), None)
            if step is None:
                step = {"kind": "tool", "tool": {"id": identity, "name": event.get("tool_name", "tool"), "status": "queued"}}
                record["steps"].append(step)
            tool = step["tool"]
            if event.get("tool_name"):
                tool["name"] = event["tool_name"]
            if "input" in event:
                tool["input"] = event["input"]
            if kind == "tool_started":
                tool.update(status="running", startedAt=event.get("timestamp") or int(time.time() * 1000))
            elif kind == "tool_result":
                if event.get("media"):
                    tool["media"] = copy.deepcopy(event["media"])
                tool.update(status="cancelled" if event.get("status") == "cancelled" else "error" if event.get("is_error") else "completed",
                            result=str(event.get("result", event.get("content", "")))[:16000],
                            finishedAt=event.get("timestamp") or int(time.time() * 1000))
        elif kind == "log":
            text = re.sub(r"\[/?(?:bold|dim|red|green|yellow|cyan|magenta|blue)(?:[ a-z]*)\]", "", str(event.get("text", "")))
            if text.startswith("上下文已压缩：") and any(step.get("kind") == "compaction" for step in record["steps"]): return False
            if not re.search(r"compaction complete|Context collapse applied|上下文.*压缩", text, re.I):
                return False
            self.flush()
            record["steps"].append({"kind": "text", "text": text.strip()})
        elif kind in {"tool_protocol_error", "model_retry", "workflow_incomplete"}:
            if event.get("agent_id"): return False
            self.flush()
            retrying = event.get("retrying", kind == "model_retry")
            record["steps"].append({"kind": "notice", "notice": {
                "status": "retrying" if retrying else "error", "reason": kind,
                **{key: event[key] for key in ("message", "attempt", "limit", "detail", "diagnostic") if key in event}}})
            record["runtimeStatus"] = "retrying" if retrying else "error"
        elif kind == "loop_transition" and event.get("reason") in {"model_fallback", "stop_hook_blocking"}:
            if event.get("agent_id"): return False
            labels = {"model_fallback": "模型请求失败，正在使用已配置的后备连接重试。",
                "stop_hook_blocking": "结束检查未通过，正在请求模型继续。"}
            return self.accept({"type": "model_retry", "message": labels[event["reason"]],
                **({"attempt": event["retries"]} if "retries" in event else {})})
        elif kind == "loop_transition" and event.get("reason") in {"continuation_substitution", "continuation_deferred"}:
            self.flush()
            detail = event.get("detail", "")
            reasons = {"guard_unknown": "条件所需结果不完整", "ambiguous_candidates": "后续动作需要进一步判断",
                "new_input_or_scope_changed": "收到新输入或执行范围变化", "tool_or_scope_changed": "工具或执行范围变化",
                "recovery_uncertain": "中断操作需要核对"}
            text = ("按已声明计划执行 · " + str(event.get("node_id", "")) + " · 未新增模型请求"
                if event["reason"] == "continuation_substitution" else "交回模型处理 · " + reasons.get(detail, detail))
            record["steps"].append({"kind": "text", "text": text})
        elif kind == "subagents_waiting":
            record["runtimeStatus"] = "waiting_subagents" if event.get("count") else "waiting"
            self.flush()
            record["steps"].append({"kind": "text", "text": event["message"]})
        elif kind == "permission":
            classifier = event.get("request", {}).get("classifier_input", {})
            if classifier.get("agent_role", "main") != "main":
                return False
            identity = "main:" + str(classifier.get("tool_use_id"))
            for step in record["steps"]:
                if step.get("tool", {}).get("id") == identity:
                    step["tool"].update(status="waiting", waitingRequestId=event.get("requestId"))
        elif kind == "interaction_resolved":
            for step in record["steps"]:
                tool = step.get("tool", {})
                if tool.get("waitingRequestId") == event.get("requestId") and tool.get("status") == "waiting":
                    tool["status"] = "running"
        elif kind == "final":
            self.progress("completed")
            record["outputChannel"] = "answer"
            record["answer"] = self.answer_prefix + event["text"] if event.get("text") else record["pendingText"]
            record["pendingText"] = ""
        elif kind in {"turn_finished", "turn_error", "turn_cancelled"}:
            if kind != "turn_finished":
                if kind in {"turn_cancelled", "turn_error"} and record.get("outputChannel") != "commentary" and record["pendingText"].strip():
                    record["answer"] = record["pendingText"]
                    record["pendingText"] = ""
                self.flush()
                record["status"] = "cancelled" if kind == "turn_cancelled" else "error"
            elif record["status"] == "running":
                record["status"] = "completed"
            record["finishedAt"] = int(time.time() * 1000)
            record["elapsedMs"] = self.elapsed_before_resume + max(0, int((time.monotonic() - self.clock) * 1000))
            record.pop("runtimeStatus", None)
            for step in record["steps"]:
                notice = step.get("notice", {})
                if notice.get("status") == "retrying":
                    notice["status"] = "cancelled" if record["status"] == "cancelled" else "interrupted" if record["status"] != "completed" else "completed"
                if step.get("kind") == "compaction" and step["compaction"].get("status") == "running":
                    step["compaction"]["status"] = "cancelled" if record["status"] == "cancelled" else "interrupted"
                tool = step.get("tool", {})
                if tool.get("status") in {"running", "queued", "waiting"}:
                    tool["status"] = "cancelled" if record["status"] == "cancelled" else "interrupted"
        else:
            return False
        return True


def visible_text(message):
    if message.get("role") == "user" and isinstance(message.get("_display_text"), str):
        return message["_display_text"]
    content = message.get("content", "")
    text = content if isinstance(content, str) else "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = text.split("\n\n<selected-skills>")[0]
    if message.get("role") == "assistant":
        position = marker_position(text)
        if position is not None:
            calls = isinstance(content, list) and any(b.get("type") == "tool_use" for b in content if isinstance(b, dict))
            text = text[:position].rstrip() + ("" if calls else "\n\n上次回复中的工具调用未执行，请重新发送任务。")
    return text


def conversation_history(messages, traces=(), *, boundaries=False):
    """Group tool-loop messages into user turns; retain legacy CLI histories."""
    saved = {t.get("userMessageId"): t for t in traces if t.get("userMessageId")}
    items, group, user = [], [], None

    def finish(end):
        if not group and not saved.get((user or {}).get("_uuid")):
            return
        trace = copy.deepcopy(saved.get((user or {}).get("_uuid")))
        assistants = [m for m in group if m.get("role") == "assistant"]
        answer = visible_text(assistants[-1]) if assistants else ""
        if trace:
            if trace.get("status") == "running":
                trace["status"] = "interrupted"
                if trace.get("pendingText", "").strip():
                    trace["steps"].append({"kind": "text", "text": trace["pendingText"]})
                trace["pendingText"] = ""
            answer = trace.get("answer", "")
        elif assistants:
            tools = {t["id"]: t for t in transcript_activity(group)}
            steps = []
            final = assistants[-1]
            has_calls = any(b.get("type") == "tool_use" for b in final.get("content", []) if isinstance(b, dict))
            for m in assistants:
                text = visible_text(m)
                if text.strip() and (m is not final or has_calls):
                    steps.append({"kind": "text", "text": text})
                for block in m.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool = tools.get("main:" + block.get("id", ""))
                        if tool:
                            steps.append({"kind": "tool", "tool": tool})
            if has_calls:
                answer = ""
            if steps or timestamp(user or {}) and timestamp(final):
                trace = {"id": (user or {}).get("_uuid", "legacy"), "status": "interrupted" if has_calls else "completed",
                         "steps": steps, "startedAt": timestamp(user or {}), "approximate": True}
                if not has_calls:
                    trace["finishedAt"] = timestamp(final)
        if answer.strip() or trace:
            if trace and "files" not in trace:
                trace["files"] = legacy_changes([step["tool"] for step in trace["steps"] if step.get("kind") == "tool"], os.getcwd())
            items.append({"role": "assistant", "text": answer, **({"trace": trace} if trace else {}),
                          **({"_messageEnd": end, "_userId": (user or {}).get("_uuid")} if boundaries else {})})

    for index, message in enumerate(messages):
        if message.get("_is_meta") or message.get("_origin") in {"team", "subagent"}:
            continue
        content = message.get("content", "")
        human = message.get("role") == "user" and (isinstance(content, str) or
            isinstance(content, list) and not any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)))
        if human and visible_text(message).strip():
            finish(index)
            group, user = [], message
            items.append({"role": "user", "text": visible_text(message), **({"id": message["_uuid"]} if message.get("_uuid") else {}), **({"attachments": copy.deepcopy(message["_attachments"])} if message.get("_attachments") else {})})
        elif message.get("role") in {"assistant", "user"}:
            group.append(message)
    finish(len(messages))
    return items
