import { useEffect, useRef, useState } from "react";
import { ChevronRight, FileInput, GitBranch, RefreshCw } from "lucide-react";
import { t, tServer, tx } from "./i18n";

type Stage = { stage_id: string; status: string; liveness?: string; agent_id?: string; error?: string };
type Run = { run_id: string; status: string; liveness?: string; items: { item_id: string; status: string; stages: Stage[] }[] };
const labels: Record<string, string> = { completed: t("已完成"), running: t("执行中"), queued: t("排队中"), waiting: t("待执行"),
  blocked: t("需处理"), interrupted: t("已中断"), awaiting_resume: t("等待恢复"), cancelled: t("已停止"), crashed: t("失败"), closed: t("已关闭") };

export default function PipelineTree({ sessionId, busy, invoke, onError, onHistory }: {
  sessionId: string; busy: boolean;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  onError: (error: unknown) => void;
  onHistory: (id: string, name: string) => void;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const refresh = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    let disposed = false, inFlight = false;
    const current = ++generation.current;
    setRuns([]);
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const data = await invoke("agents.tree");
        if (!disposed && generation.current === current) setRuns(data.runs);
      } catch (error) { if (!disposed) onError(error); }
      finally { inFlight = false; }
    };
    refresh.current = load;
    void load();
    const timer = setInterval(() => void load(), 2500);
    return () => { disposed = true; clearInterval(timer); };
  }, [sessionId]);
  return <section className="pipeline-tree" aria-label={t("协作运行树")}>
    <div className="card-heading"><GitBranch size={14} /><strong>{t("协作运行树")}</strong>
      <button className="icon-button" aria-label={t("刷新运行树")} onClick={() => void refresh.current()}><RefreshCw size={13} /></button>
    </div>
    <input hidden type="file" accept=".json,application/json" ref={input} onChange={async (event) => {
      const file = event.target.files?.[0]; event.target.value = "";
      if (!file) return;
      try {
        const spec = JSON.parse(await file.text());
        await invoke("agents.pipeline", { runId: "run-" + crypto.randomUUID(), spec });
        await refresh.current();
      } catch (error) { onError(error); }
    }} />
    <button className="text-button" disabled={busy} onClick={() => input.current?.click()}><FileInput size={13} />{t("导入流水线 JSON")}</button>
    {!runs.length && <small>{t("每项依次推进，各项并行执行。运行状态取自实际子代理记录。")}</small>}
    {runs.map(run => <details className="pipeline-run" key={run.run_id} open>
      <summary title={run.run_id}><ChevronRight className="pipeline-chevron" size={12} /><span>{tx("流水线 · {length} 项", { length: run.items.length })}</span><small>{labels[run.status] || run.status}{run.liveness === "unconfirmed" && t(" · 存活未确认")}</small></summary>
      {run.items.map(item => <details key={item.item_id} open={item.status !== "completed"}>
        <summary><ChevronRight className="pipeline-chevron" size={12} /><span>{item.item_id}</span><small>{labels[item.status] || item.status}</small></summary>
        {item.stages.map(stage => <div className="pipeline-stage" key={stage.stage_id}>
          <button className="text-button" disabled={!stage.agent_id} title={stage.error || stage.agent_id || t("尚未启动")}
            onClick={() => stage.agent_id && onHistory(stage.agent_id, item.item_id + " / " + stage.stage_id)}>{stage.stage_id}</button>
          <small>{labels[stage.status] || stage.status}{stage.liveness === "unconfirmed" && t(" · 记录状态")}</small>
          {stage.error && <small className="bad">{tServer(stage.error)}</small>}
        </div>)}
      </details>)}
      {run.status !== "completed" && (run.status !== "running" || run.liveness === "unconfirmed") && <button className="text-button" disabled={busy}
        onClick={() => invoke("agents.pipeline", { runId: run.run_id }).then(() => refresh.current()).catch(onError)}>{run.liveness === "unconfirmed" ? t("核对并继续未完成步骤") : t("继续未完成步骤")}</button>}
      {run.status === "blocked" && <small>{t("已中断的步骤需先核对结果并显式继续对应子代理，不会自动重跑。")}</small>}
    </details>)}
  </section>;
}
