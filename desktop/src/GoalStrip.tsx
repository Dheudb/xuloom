import { useEffect, useState } from "react";
import { Target, PauseCircle, PlayCircle, Trash2, Pencil, LoaderCircle, X } from "lucide-react";
import type { SessionView } from "./types";
import { t } from "./i18n";

const labels: Record<string, string> = { active: t("进行中的目标"), paused: t("已暂停的目标"), blocked: t("目标需要处理"), interrupted: t("已中断的目标"), awaiting_review: t("目标等待验收"), completed: t("目标已完成"), budget_exhausted: t("目标已达运行上限") };
function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds)), hours = Math.floor(value / 3600), minutes = Math.floor(value % 3600 / 60);
  return t("{value}{value2}{value3}秒", { value: hours ? t("{hours}小时", { hours }) : "", value2: minutes ? t("{minutes}分", { minutes }) : "", value3: value % 60 });
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
  const acceptance: Record<string, string> = { model_report: t("模型报告完成"), host_validator: t("程序检查通过"), declared_file_checks: t("文件检查通过"), user_or_host: t("用户或接入程序已验收") };
  const completionLabel = acceptance[goal.accepted_by || ""] || t("已结束 · 未记录验收依据");
  const elapsed = (goal.elapsed_seconds || 0) + (busy && goal.status === "active" && goal.phase === "executing" && goal.round_started_at ? Math.max(0, now / 1000 - goal.round_started_at) : 0);
  async function control(action: "pause" | "cancel" | "resume") {
    setPending(true);
    try { await invoke(action === "resume" ? "goal.run" : "goal.control", action === "resume" ? {} : { action }); }
    catch (error) { onError(error); }
    finally { setPending(false); }
  }
  const limits = [goal.budget?.seconds ? t("执行时间 {value} / {value2}", { value: duration(elapsed), value2: duration(goal.budget.seconds) }) : null,
    goal.budget?.rounds ? t("轮数 {value} / {rounds}", { value: goal.rounds || 0, rounds: goal.budget.rounds }) : null,
    goal.budget?.output_tokens ? t("输出 {value} / {output_tokens} tokens", { value: goal.output_used || 0, output_tokens: goal.budget.output_tokens }) : null].filter(Boolean).join(' · ');
  return <div>{limits && <p className="fine-print" aria-label={t("目标预算用量")}>{limits}</p>}<div className="goal-strip" role="region" aria-label={t("持续目标")}>
    <Target size={15} />
    <button className="goal-strip-summary" aria-label={completed ? t("查看目标完成依据") : t("管理当前目标")} title={goal.objective} aria-expanded={completed ? evidence : undefined} onClick={() => completed ? setEvidence(!evidence) : details()}><strong>{completed ? completionLabel : labels[goal.status] || t("当前目标")}</strong><span>{goal.objective}</span></button>
    <span className="goal-strip-time" title={t("累计执行时间；暂停时间不计入")}>{duration(elapsed)}</span>
    {pending && <LoaderCircle size={14} className="spin" />}
    {completed ? <button className="icon-button" aria-label={t("收起已完成目标")} title={t("收起记录；验收依据仍保存在会话中")} onClick={() => setDismissed(true)}><X size={14} /></button> : <button className="icon-button" aria-label={t("取消持续目标")} title={t("取消持续目标")} disabled={pending} onClick={() => void control("cancel")}><Trash2 size={14} /></button>}
    {busy && goal.status === "active" ? <button className="icon-button" aria-label={t("暂停持续目标")} title={t("暂停持续目标")} disabled={pending} onClick={() => void control("pause")}><PauseCircle size={15} /></button>
      : !["awaiting_review", "completed", "budget_exhausted"].includes(goal.status) && <button className="icon-button" aria-label={t("恢复持续目标")} title={t("恢复持续目标")} disabled={pending || busy} onClick={() => void control("resume")}><PlayCircle size={15} /></button>}
    <button className="icon-button" aria-label={t("调整目标")} title={t("调整目标")} onClick={details}><Pencil size={14} /></button>
  </div>{completed && evidence && <div className="goal-completion-details" role="region" aria-label={t("目标完成依据")}>
    <strong>{completionLabel}</strong><p>{goal.reason || t("此历史记录没有保存详细的完成说明。")}</p>
    {goal.accepted_by === "model_report" && <p>{t("这是模型对任务完成情况的报告，未经过独立程序检查或用户验收。")}</p>}
    {!!goal.check_results?.length && <ul>{goal.check_results.map((check, i) => <li key={i}>{check.passed ? t("通过") : t("未通过")} · {check.path}</li>)}</ul>}
  </div>}</div>;
}
