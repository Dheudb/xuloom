import { useEffect, useState } from "react";
import { ChevronDown, LoaderCircle, ListCollapse, RotateCw, Workflow } from "lucide-react";
import type { TurnTrace, ToolActivity, CompactionNotice, RetryNotice } from "./types";
import { turnPhase } from "./turn-phase";
import Markdown from "./Markdown";
import { statusText } from "./ActivityPanel";
import { toolPresentation } from "./tool-presentation";
import ToolIcon from "./ToolIcon";
import ToolImages from "./ToolImages";

export function durationText(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  return `${hours ? hours + "小时" : ""}${minutes ? minutes + "分" : ""}${seconds}秒`;
}

function ProcessTool({ tool, sessionId }: { tool: ToolActivity; sessionId: string }) {
  const view = toolPresentation(tool), pending = ["running", "queued"].includes(tool.status);
  const elapsed = tool.startedAt && tool.finishedAt && tool.finishedAt - tool.startedAt >= 1000 ? durationText(tool.finishedAt - tool.startedAt) : "";
  return <div className="turn-tool-entry">
    <details className={"turn-tool " + (tool.status === "error" ? "failed" : tool.status)} data-tool-status={tool.status}>
      <summary title={view.brief}>
        <ToolIcon kind={view.kind} /><span className="turn-tool-title">{view.title}</span>
        <span className="turn-tool-brief">{view.brief}</span>
        {pending && <LoaderCircle size={12} className="spin turn-tool-spinner" aria-label="正在执行" />}
        {tool.status === "waiting" && <span className="turn-tool-status">{statusText[tool.status]}</span>}
        {elapsed && <span className="turn-tool-duration">{elapsed}</span>}<ChevronDown size={12} className="turn-tool-chevron" />
      </summary>
      <div className="turn-tool-detail"><small>调用参数</small><pre>{JSON.stringify(tool.input || {}, null, 2)}</pre><small>执行结果</small><div className="markdown"><Markdown text={tool.result || "尚未返回结果。"} sessionId={sessionId} allowImages={false} /></div></div>
    </details>
    <ToolImages media={tool.media} sessionId={sessionId} />
  </div>;
}

function CompactStatus({ notice, turnStatus }: { notice: CompactionNotice; turnStatus: string }) {
  const status = notice.status === "running" && turnStatus !== "running" ? turnStatus === "cancelled" ? "cancelled" : "interrupted" : notice.status;
  const title = notice.before !== undefined && notice.after !== undefined ? `约 ${notice.before.toLocaleString()} → ${notice.after.toLocaleString()} tokens · ${durationText(notice.elapsedMs || 0)}` : undefined;
  return <div className="turn-compaction" data-compaction-id={notice.id} data-compaction-status={status} role={turnStatus === "running" ? "status" : undefined} title={title}>
    <ListCollapse size={16} /><span>{{ running: "正在压缩上下文…", completed: "上下文已自动压缩", failed: "上下文压缩失败，原始记录已保留", cancelled: "上下文压缩已取消", interrupted: "上下文压缩已中断" }[status]}</span>
    {status === "running" && <LoaderCircle size={13} className="spin" />}
  </div>;
}

function RetryStatus({ notice, turnStatus }: { notice: RetryNotice; turnStatus: string }) {
  const status = notice.status === "retrying" && turnStatus !== "running" ? turnStatus === "completed" ? "completed" : turnStatus === "cancelled" ? "cancelled" : "interrupted" : notice.status;
  const suffix = ({ retrying: "正在重试", completed: "已重试", error: "未能恢复", cancelled: "已停止", interrupted: "已中断" } as Record<string, string>)[status];
  if (notice.reason === "collaboration_decision") return <details className="turn-retry turn-decision">
    <summary><Workflow size={15} /><span>{notice.title}</span><ChevronDown size={12} /></summary>
    <div className="turn-retry-detail"><p>{notice.message}</p>{notice.detail && <pre>{notice.detail}</pre>}</div>
  </details>;
  return <details className="turn-retry" data-retry-status={status}>
    <summary><RotateCw size={15} className={status === "retrying" ? "spin" : ""} /><span>Retry{notice.attempt !== undefined ? ` ${Math.min(notice.attempt, notice.limit || notice.attempt)}${notice.limit ? `/${notice.limit}` : ""}` : ""}</span><span>{suffix}</span><ChevronDown size={12} /></summary>
    <div className="turn-retry-detail"><p>{notice.message}</p>{notice.detail && <pre>{notice.detail}</pre>}{notice.diagnostic && <pre>{notice.diagnostic}</pre>}</div>
  </details>;
}

export default function TurnProcess({ trace, sessionId }: { trace: TurnTrace; sessionId: string }) {
  const running = trace.status === "running";
  const [open, setOpen] = useState(running);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { setOpen(running); }, [running]);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const elapsed = trace.elapsedMs ?? (trace.resumedAt && running ? (trace.elapsedBeforeResumeMs || 0) + Math.max(0, now - trace.resumedAt) : trace.startedAt && (trace.finishedAt || (running ? now : 0))
    ? Math.max(0, (trace.finishedAt || now) - trace.startedAt) : undefined);
  const state = running ? "执行中" : trace.status === "cancelled" ? "已停止" : ["error", "interrupted"].includes(trace.status) ? "已中断" : "";
  const stream = trace.modelStreams?.at(-1);
  const steps = trace.steps.filter(step => step.kind !== "text" || step.protocolError || step.text.trim());
  const notices = trace.steps.filter((step) => step.kind === "compaction");
  const phase = turnPhase(trace);
  const reasoning = stream?.reasoningTokens !== undefined ? `接口报告推理 ${stream.reasoningTokens} tokens` : stream?.reasoningItems ? `接口返回 ${stream.reasoningItems} 项推理记录` : `推理数据 ${stream?.reasoningChunks || 0} 段（流片段，非 Token）`;
  const timing = stream ? `本轮 ${trace.modelStreams?.length} 次模型请求；最近一次：${stream.firstTextMs !== undefined ? `首个文本 ${(stream.firstTextMs / 1000).toFixed(1)} 秒` : "尚未收到文本"}；已收到 ${stream.textChars || 0} 字符；${reasoning}${stream.finishReason ? `；结束标记 ${stream.finishReason}` : ""}${stream.error ? `；${stream.error}` : ""}` : undefined;
  const label = <span title={timing}>{trace.status === "cancelled" && elapsed !== undefined
    ? `你在 ${durationText(elapsed)} 后停止了`
    : <>{state}{state && elapsed !== undefined ? " · " : ""}{elapsed !== undefined ? `${trace.approximate ? "约" : ""}用时 ${durationText(elapsed)}` : state ? "" : "执行过程"}{phase ? ` · ${phase}` : ""}</>}</span>;
  const commentary = trace.outputChannel === "commentary" ? trace.pendingText?.trim() : "";
  if (!commentary && !trace.steps.some((step) => step.kind === "tool" || step.kind === "notice" || step.kind === "text" && step.protocolError)) {
    return elapsed === undefined && !state && !notices.length ? null : <div className="turn-process turn-process-time" data-turn-status={trace.status}>{label}
      {notices.map(step => <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />)}</div>;
  }
  return <><details className="turn-process" open={open} onToggle={(event) => setOpen(event.currentTarget.open)} data-turn-status={trace.status}>
    <summary aria-label="查看本轮执行过程">
      {label}
      <ChevronDown size={14} /><span className="turn-step-count">{steps.length ? `${steps.length} 项过程` : ""}</span>
    </summary>
    <div className="turn-process-content">
      {steps.map((step, index) => step.kind === "notice"
        ? <RetryStatus key={index} notice={step.notice} turnStatus={trace.status} />
        : step.kind === "text"
        ? step.protocolError ? <RetryStatus key={index} notice={{ status: "completed", message: step.text, ...step.protocolError }} turnStatus={trace.status} />
          : <div className="turn-commentary markdown" key={index}><Markdown text={step.text} sessionId={sessionId} /></div>
        : step.kind === "compaction" ? <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />
        : <ProcessTool tool={step.tool} sessionId={sessionId} key={step.tool.id || index} />)}
      {commentary && <div className="turn-commentary markdown"><Markdown text={commentary} sessionId={sessionId} /></div>}
    </div>
  </details>{!open && notices.map(step => <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />)}</>;
}
