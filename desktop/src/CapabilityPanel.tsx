import { useEffect, useState } from "react";
import {
  RefreshCw,
  Bot,
  Square,
  FileDiff,
  Activity,
  FolderTree,
  ChartPie,
  PanelRight,
} from "lucide-react";
import type { SessionView, FileChange } from "./types";
import ChangesPanel from "./ChangesPanel";
import Markdown from "./Markdown";
import ActivityPanel from "./ActivityPanel";
import WorkspaceTree from "./WorkspaceTree";
import WorkerHistory from "./WorkerHistory";
import PipelineTree from "./PipelineTree";
import ContextPanel from "./ContextPanel";
import { t, tx } from "./i18n";

export default function CapabilityPanel({
  session,
  tab,
  setTab,
  invoke,
  refresh,
  update,
  onError,
  onOpenFile,
  onNavigateChat,
  onManage,
  activityQuery,
  contextBusy,
}: {
  session?: SessionView;
  tab: string;
  setTab: (tab: string) => void;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  refresh: () => Promise<void>;
  update: (patch: Partial<SessionView>) => void;
  onError: (e: unknown) => void;
  onOpenFile: (path: string, change?: FileChange) => void;
  onNavigateChat: (url: string) => void;
  onManage: (kind: "skills" | "agents") => void;
  activityQuery?: string;
  contextBusy: boolean;
}) {
  const [historyWorker, setHistoryWorker] = useState<{ id: string; name: string; remoteUrl?: string } | null>(null);
  useEffect(() => setHistoryWorker(null), [session?.id]);
  return (
    <aside className="inspector">
      <div className="panel-tabs">
        {[
          { id: "changes", label: t("变更"), icon: FileDiff },
          { id: "activity", label: t("活动"), icon: Activity },
          { id: "agents", label: t("代理"), icon: Bot },
          { id: "files", label: t("文件"), icon: FolderTree },
          { id: "context", label: t("上下文"), icon: ChartPie },
        ].map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            title={label}
            aria-label={label}
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
          >
            <Icon size={14} />
            <span>{label}</span>
          </button>
        ))}
      </div>
      {!session ? (
        <div className="panel-empty">
          <PanelRight size={28} />
          <p>{t("任务结果会显示在这里")}</p>
          <small>{t("开始会话后，查看变更、执行活动、子代理和文件。")}</small>
        </div>
      ) : (
        <>
          {tab === "agents" && (
            <>
              <div className="panel-title">
                <h3>{t("子代理")}</h3>
                <button className="icon-button" aria-label={t("刷新子代理")} onClick={() => refresh().catch(onError)}><RefreshCw size={15} /></button>
              </div>
              <div className="panel-scroll">
                <PipelineTree sessionId={session.id} busy={session.busy || !!session.controlledExecution} invoke={invoke} onError={onError}
                  onHistory={(id, name) => setHistoryWorker({ id, name })} />
                {session.workers.length > 0 ? session.workers.map((worker) => (
                      <article
                        className="capability-card"
                        key={worker.agent_id}
                      >
                        <div className="card-heading">
                          <Bot size={15} />
                          <strong>{worker.description || worker.agent_type || t("子代理")}</strong>
                          <span className={"status-dot " + worker.status} />
                        </div>
                        <p>{worker.task}</p>
                        <div className="card-meta worker-meta">
                          <span className="worker-model" title={worker.model}>{worker.agent_type || t("子代理")}{worker.backend === "claude" ? " · Claude Code" : worker.backend === "pi" ? " · Pi" : worker.backend === "codex" ? " · Codex" : worker.backend === "dsh" ? " · DSH" : ""} · {worker.model || (worker.executor?.startsWith("ssh:") ? t("执行主机默认模型") : t("继承主模型"))}</span>
                          <span className="worker-status">
                            {worker.outcome_unknown ? t("结果待确认") : worker.status === "queued" ? worker.phase === "awaiting_resume" ? t("等待恢复队列") : t("排队中") : worker.status === "running"
                              ? worker.phase === "starting" ? t("启动中") : t("执行中")
                              : worker.status === "completed"
                                ? t("已完成")
                                : worker.status === "cancelled"
                                  ? t("已取消")
                                  : t("异常")}
                          </span>
                        </div>
                        {worker.started_at && <div className="card-meta"><span>{tx("启动 {value}", { value: new Date(worker.started_at).toLocaleTimeString("zh-CN", { hour12: false }) })}</span></div>}
                        {!!worker.pending_message_count && <p className="fine-print">{tx("{pending_message_count} 条消息待处理{value}", { pending_message_count: worker.pending_message_count, value: worker.status !== "running" && t(" · 请在主会话继续此代理") })}</p>}
                        {worker.external_recovery && <p className="fine-print">{worker.external_recovery.resumable ? t("停止已确认，可在主会话继续此代理。") : t("停止结果尚未确认，请先核对已有产物，不会自动重放。")}</p>}
                        {worker.remote_reference && <div className="card-meta" title={worker.remote_reference.cwd}><span>SSH · {worker.remote_reference.machine || worker.remote_reference.host}</span><button className="text-button" onClick={() => onNavigateChat(worker.remote_reference!.url)}>{t("查看远端历史与文件")}</button></div>}
                        <button className="text-button" onClick={() => setHistoryWorker({ id: worker.agent_id, name: worker.description || worker.agent_type || t("子代理"), remoteUrl: worker.remote_reference?.url })}>{t("查看完整对话历史")}</button>
                        {worker.status === "queued" && worker.phase === "awaiting_resume" && <button className="text-button" disabled={session.busy}
                          onClick={() => invoke("agents.resume_queue", { agentIds: [worker.agent_id] }).then(refresh).catch(onError)}>{t("恢复排队")}</button>}
                        {(["queued", "running"].includes(worker.status) || worker.outcome_unknown) && (
                          <button
                            className="text-button"
                            onClick={() =>
                              invoke("agents.cancel", {
                                agentId: worker.agent_id,
                              }).catch(onError)
                            }
                          >
                            <Square size={12} />{t("停止代理")}</button>
                        )}
                        {worker.result && (
                          <details>
                            <summary>{t("查看结果")}</summary>
                            <div className="worker-result">
                              <Markdown text={worker.result} sessionId={session.id} remoteUrl={worker.remote_reference?.url} />
                            </div>
                          </details>
                        )}
                      </article>
                )) : (
                  <div className="instruction-empty">
                    <Bot size={26} />
                    <p>{t("本会话还没有子代理任务。")}</p>
                    <button className="text-button" onClick={() => onManage("agents")}>{t("管理子代理角色")}</button>
                  </div>
                )}
              </div>
            </>
          )}
          {tab === "activity" && (
            <ActivityPanel session={session} initialQuery={activityQuery} />
          )}
          {tab === "changes" && <ChangesPanel key={session.id} session={session} invoke={invoke} onOpen={onOpenFile} />}
          {tab === "files" && <WorkspaceTree sessionId={session.id} project={session.project} changes={session.fileChanges} onOpen={onOpenFile} />}
          {tab === "context" && <ContextPanel invoke={invoke} busy={contextBusy} working={!!session.contextCompacting}
            model={session.config} compactError={session.contextCompactError}
            setCompaction={(contextCompacting, contextCompactError) => update({ contextCompacting, contextCompactError })} />}
        </>
      )}
      {historyWorker && <WorkerHistory key={session?.id + ":" + historyWorker.id} agentId={historyWorker.id} name={historyWorker.name} remoteUrl={historyWorker.remoteUrl} invoke={invoke} close={() => setHistoryWorker(null)} />}
    </aside>
  );
}
