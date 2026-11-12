import { useEffect, useState } from "react";
import { ChevronDown, LoaderCircle, ListCollapse, RotateCw, Workflow } from "lucide-react";
import type { TurnTrace, ToolActivity, CompactionNotice, RetryNotice } from "./types";
import { turnPhase } from "./turn-phase";
import Markdown from "./Markdown";
import { statusText } from "./ActivityPanel";
import { toolPresentation } from "./tool-presentation";
import ToolIcon from "./ToolIcon";
import ToolImages from "./ToolImages";
import { usageText } from "./CostSettings";
import { t, tServer } from "./i18n";

export function durationText(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), seconds = total % 60;
  return t("{value}{value2}{seconds}秒", { value: hours ? t("{hours}小时", { hours }) : "", value2: minutes ? t("{minutes}分", { minutes }) : "", seconds });
}

function ProcessTool({ tool, sessionId }: { tool: ToolActivity; sessionId: string }) {
  const view = toolPresentation(tool), pending = ["running", "queued"].includes(tool.status);
  const elapsed = tool.startedAt && tool.finishedAt && tool.finishedAt - tool.startedAt >= 1000 ? durationText(tool.finishedAt - tool.startedAt) : "";
  return <div className="turn-tool-entry">
    <details className={"turn-tool " + (tool.status === "error" ? "failed" : tool.status)} data-tool-status={tool.status}>
      <summary title={view.brief}>
        <ToolIcon kind={view.kind} /><span className="turn-tool-title">{view.title}</span>
        <span className="turn-tool-brief">{view.brief}</span>
        {pending && <LoaderCircle size={12} className="spin turn-tool-spinner" aria-label={t("正在执行")} />}
        {tool.status === "waiting" && <span className="turn-tool-status">{statusText[tool.status]}</span>}
        {elapsed && <span className="turn-tool-duration">{elapsed}</span>}<ChevronDown size={12} className="turn-tool-chevron" />
      </summary>
      <div className="turn-tool-detail"><small>{t("调用参数")}</small><pre>{JSON.stringify(tool.input || {}, null, 2)}</pre><small>{t("执行结果")}</small><div className="markdown"><Markdown text={tool.result || t("尚未返回结果。")} sessionId={sessionId} allowImages={false} /></div></div>
    </details>
    <ToolImages media={tool.media} sessionId={sessionId} />
  </div>;
}

function CompactStatus({ notice, turnStatus }: { notice: CompactionNotice; turnStatus: string }) {
  const status = notice.status === "running" && turnStatus !== "running" ? turnStatus === "cancelled" ? "cancelled" : "interrupted" : notice.status;
  const title = notice.before !== undefined && notice.after !== undefined ? t("约 {before} → {after} tokens · {value}", { before: notice.before.toLocaleString(), after: notice.after.toLocaleString(), value: durationText(notice.elapsedMs || 0) }) : undefined;
  return <div className="turn-compaction" data-compaction-id={notice.id} data-compaction-status={status} role={turnStatus === "running" ? "status" : undefined} title={title}>
    <ListCollapse size={16} /><span>{{ running: t("正在压缩上下文…"), completed: t("上下文已自动压缩"), failed: t("上下文压缩失败，原始记录已保留"), cancelled: t("上下文压缩已取消"), interrupted: t("上下文压缩已中断") }[status]}</span>
    {status === "running" && <LoaderCircle size={13} className="spin" />}
  </div>;
}

function RetryStatus({ notice, turnStatus }: { notice: RetryNotice; turnStatus: string }) {
  const status = notice.status === "retrying" && turnStatus !== "running" ? turnStatus === "completed" ? "completed" : turnStatus === "cancelled" ? "cancelled" : "interrupted" : notice.status;
  const suffix = ({ retrying: t("正在重试"), completed: t("已重试"), error: t("未能恢复"), cancelled: t("已停止"), interrupted: t("已中断") } as Record<string, string>)[status];
  if (notice.reason === "collaboration_decision") return <details className="turn-retry turn-decision">
    <summary><Workflow size={15} /><span>{notice.title}</span><ChevronDown size={12} /></summary>
    <div className="turn-retry-detail"><p>{tServer(notice.message)}</p>{notice.detail && <pre>{notice.detail}</pre>}</div>
  </details>;
  return <details className="turn-retry" data-retry-status={status}>
    <summary><RotateCw size={15} className={status === "retrying" ? "spin" : ""} /><span>Retry{notice.attempt !== undefined ? ` ${Math.min(notice.attempt, notice.limit || notice.attempt)}${notice.limit ? `/${notice.limit}` : ""}` : ""}</span><span>{suffix}</span><ChevronDown size={12} /></summary>
    <div className="turn-retry-detail"><p>{tServer(notice.message)}</p>{notice.detail && <pre>{notice.detail}</pre>}{notice.diagnostic && <pre>{notice.diagnostic}</pre>}</div>
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
  const state = running ? t("执行中") : trace.status === "cancelled" ? t("已停止") : ["error", "interrupted"].includes(trace.status) ? t("已中断") : "";
  const stream = trace.modelStreams?.at(-1);
  const steps = trace.steps.filter(step => step.kind !== "text" || step.protocolError || step.text.trim());
  const notices = trace.steps.filter((step) => step.kind === "compaction");
  const phase = turnPhase(trace);
  const reasoning = stream?.reasoningTokens !== undefined ? t("接口报告推理 {reasoningTokens} tokens", { reasoningTokens: stream.reasoningTokens }) : stream?.reasoningItems ? t("接口返回 {reasoningItems} 项推理记录", { reasoningItems: stream.reasoningItems }) : t("推理数据 {value} 段（流片段，非 Token）", { value: stream?.reasoningChunks || 0 });
  const timing = stream ? t("本轮 {value} 次模型请求；最近一次：{value2}；已收到 {value3} 字符；{reasoning}{value4}{value5}", { value: trace.modelStreams?.length, value2: stream.firstTextMs !== undefined ? t("首个文本 {value} 秒", { value: (stream.firstTextMs / 1000).toFixed(1) }) : t("尚未收到文本"), value3: stream.textChars || 0, reasoning, value4: stream.finishReason ? t("；结束标记 {finishReason}", { finishReason: stream.finishReason }) : "", value5: stream.error ? `；${stream.error}` : "" }) : undefined;
  const label = <span title={timing}>{trace.status === "cancelled" && elapsed !== undefined
    ? t("你在 {value} 后停止了", { value: durationText(elapsed) })
    : <>{state}{state && elapsed !== undefined ? " · " : ""}{elapsed !== undefined ? t("{value}用时 {value2}", { value: trace.approximate ? t("约") : "", value2: durationText(elapsed) }) : state ? "" : t("执行过程")}{phase ? ` · ${phase}` : ""}</>}</span>;
  const usage = usageText(trace.usage);
  const usageLabel = usage && <span className="turn-usage" title={usage.title} data-turn-usage>{usage.text}</span>;
  const commentary = trace.outputChannel === "commentary" ? trace.pendingText?.trim() : "";
  if (!commentary && !steps.some((step) => step.kind !== "compaction")) {
    return elapsed === undefined && !state && !notices.length ? null : <div className="turn-process turn-process-time" data-turn-status={trace.status}>{label}{usageLabel}
      {notices.map(step => <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />)}</div>;
  }
  return <><details className="turn-process" open={open} onToggle={(event) => setOpen(event.currentTarget.open)} data-turn-status={trace.status}>
    <summary aria-label={t("查看本轮执行过程")}>
      {label}
      <ChevronDown size={14} />{usageLabel}<span className="turn-step-count">{steps.length ? t("{length} 项过程", { length: steps.length }) : ""}</span>
    </summary>
    <div className="turn-process-content">
      {steps.map((step, index) => step.kind === "notice"
        ? <RetryStatus key={index} notice={step.notice} turnStatus={trace.status} />
        : step.kind === "text"
        ? step.protocolError ? <RetryStatus key={index} notice={{ status: "completed", message: step.text, ...step.protocolError }} turnStatus={trace.status} />
          : <div className="turn-commentary markdown" key={index} data-provisional={step.provisional || undefined}>{step.provisional && <small className="turn-provisional-label">{t("阶段性内容，最终结果将在子代理结束后汇总")}</small>}<Markdown text={step.text} sessionId={sessionId} /></div>
        : step.kind === "compaction" ? <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />
        : <ProcessTool tool={step.tool} sessionId={sessionId} key={step.tool.id || index} />)}
      {commentary && <div className="turn-commentary markdown"><Markdown text={commentary} sessionId={sessionId} /></div>}
    </div>
  </details>{!open && notices.map(step => <CompactStatus key={step.compaction.id} notice={step.compaction} turnStatus={trace.status} />)}</>;
}
