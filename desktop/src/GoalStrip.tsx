import { useEffect, useState } from "react";
import { Target, PauseCircle, PlayCircle, Trash2, Pencil, LoaderCircle, X } from "lucide-react";
import type { SessionView } from "./types";

const labels: Record<string, string> = { active: "进行中的目标", paused: "已暂停的目标", blocked: "目标需要处理", interrupted: "已中断的目标", awaiting_review: "目标等待验收", completed: "目标已完成" };
function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds)), hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60);
  return `${hours ? hours + "小时" : ""}${minutes ? minutes + "分" : ""}${value % 60}秒`;
}

export default function GoalStrip({ goal, busy, invoke, onError, details }: {
  goal: NonNullable<SessionView["goal"]>; busy: boolean;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  onError: (error: unknown) => void; details: () => void;
}) {
  const [now, setNow] = useState(Date.now()), [pending, setPending] = useState(false);
  const [dismissed, setDismissed] = useState(false), [evidence, setEvidence] = useState(false);
  useEffect(() => { setDismissed(false); setEvidence(false); }, [goal.id, goal.objective, goal.status]);
  useEffect(() => {
    setNow(Date.now());
    if (!busy || goal.status !== "active") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [busy, goal.status, goal.round_started_at]);
  if (goal.status === "cancelled" || dismissed) return null;
  const completed = goal.status === "completed";
  const acceptance: Record<string, string> = { model_report: "模型报告完成", host_validator: "程序检查通过", declared_file_checks: "文件检查通过", user_or_host: "用户或接入程序已验收" };
  const completionLabel = acceptance[goal.accepted_by || ""] || "已结束 · 未记录验收依据";
  const elapsed = (goal.elapsed_seconds || 0) + (busy && goal.status === "active" && goal.phase === "executing" && goal.round_started_at ? Math.max(0, now / 1000 - goal.round_started_at) : 0);
  async function control(action: "pause" | "cancel" | "resume") {
    setPending(true);
    try { await invoke(action === "resume" ? "goal.run" : "goal.control", action === "resume" ? {} : { action }); }
    catch (error) { onError(error); }
    finally { setPending(false); }
  }
  return <div><div className="goal-strip" role="region" aria-label="持续目标">
    <Target size={15} />
    <button className="goal-strip-summary" aria-label={completed ? "查看目标完成依据" : "管理当前目标"} title={goal.objective} aria-expanded={completed ? evidence : undefined} onClick={() => completed ? setEvidence(!evidence) : details()}><strong>{completed ? completionLabel : labels[goal.status] || "当前目标"}</strong><span>{goal.objective}</span></button>
    <span className="goal-strip-time" title="累计执行时间；暂停时间不计入">{duration(elapsed)}</span>
    {pending && <LoaderCircle size={14} className="spin" />}
    {completed ? <button className="icon-button" aria-label="收起已完成目标" title="收起记录；验收依据仍保存在会话中" onClick={() => setDismissed(true)}><X size={14} /></button> : <button className="icon-button" aria-label="取消持续目标" title="取消持续目标" disabled={pending} onClick={() => void control("cancel")}><Trash2 size={14} /></button>}
    {busy && goal.status === "active" ? <button className="icon-button" aria-label="暂停持续目标" title="暂停持续目标" disabled={pending} onClick={() => void control("pause")}><PauseCircle size={15} /></button>
      : !["awaiting_review", "completed"].includes(goal.status) && <button className="icon-button" aria-label="恢复持续目标" title="恢复持续目标" disabled={pending || busy} onClick={() => void control("resume")}><PlayCircle size={15} /></button>}
    <button className="icon-button" aria-label="调整目标" title="调整目标" onClick={details}><Pencil size={14} /></button>
  </div>{completed && evidence && <div className="goal-completion-details" role="region" aria-label="目标完成依据">
    <strong>{completionLabel}</strong><p>{goal.reason || "此历史记录没有保存详细的完成说明。"}</p>
    {goal.accepted_by === "model_report" && <p>这是模型对任务完成情况的报告，未经过独立程序检查或用户验收。</p>}
    {!!goal.check_results?.length && <ul>{goal.check_results.map((check, i) => <li key={i}>{check.passed ? "通过" : "未通过"} · {check.path}</li>)}</ul>}
  </div>}</div>;
}
