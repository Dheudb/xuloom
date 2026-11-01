import { useState } from "react";
import { skillScope } from "./skill-mention";
import {
  Plus,
  RefreshCw,
  Puzzle,
  Bot,
  CircleCheck,
  CircleAlert,
  ChevronDown,
  Square,
  FileDiff,
  Activity,
  X,
  FolderTree,
} from "lucide-react";
import type { SessionView, Skill, FileChange } from "./types";
import ChangesPanel from "./ChangesPanel";
import Markdown from "./Markdown";
import CapabilityGuide from "./CapabilityGuide";
import ActivityPanel from "./ActivityPanel";
import WorkspaceTree from "./WorkspaceTree";

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
  activityQuery,
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
  activityQuery?: string;
}) {
  const [preview, setPreview] = useState<{ name: string; text: string; kind?: "agent" } | null>(
    null,
  );
  const [guide, setGuide] = useState<"skills" | "agents" | null>(null);
  async function importDirectory(kind: string) {
    try {
      const capabilities = await invoke("capabilities.import", { kind });
      if (capabilities) update({ capabilities });
    } catch (e) {
      onError(e);
    }
  }
  async function read(skill: Skill) {
    try {
      setPreview({
        name: skill.name,
        text: await invoke("skills.read", { name: skill.name }),
      });
    } catch (e) {
      onError(e);
    }
  }
  const active =
    session?.capabilities.skills.filter(
      (skill) => skill.valid && skill.enabled && !skill.shadowed,
    ) || [];
  return (
    <aside className="inspector">
      <div className="panel-tabs">
        {[
          { id: "skills", label: "Skills", icon: Puzzle },
          { id: "agents", label: "代理", icon: Bot },
          { id: "activity", label: "活动", icon: Activity },
          { id: "changes", label: "变更", icon: FileDiff },
          { id: "files", label: "文件", icon: FolderTree },
        ].map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            title={label}
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
          <Puzzle size={28} />
          <p>会话能力会显示在这里</p>
          <small>开始会话后，管理技能、子代理、文件和执行过程。</small>
        </div>
      ) : (
        <>
          {tab === "skills" && (
            <>
              <div className="panel-title">
                <div>
                  <h3>
                    技能库 <span>{active.length}</span>
                  </h3>
                  <p>按需加载，让代理掌握你的工作流</p>
                </div>
                <button
                  className="icon-button"
                  aria-label="刷新技能"
                  onClick={() => refresh().catch(onError)}
                >
                  <RefreshCw size={15} />
                </button>
              </div>
              <button
                className="import-button"
                onClick={() => importDirectory("skills")}
              >
                <Plus size={15} />
                导入技能目录
              </button>
              <button className="capability-guide-link" onClick={() => setGuide("skills")}>Skill 怎么配置？查看目录和示例</button>
              <div className="panel-scroll">
                {!session.capabilities.skills.length && (
                  <div className="instruction-empty">
                    <Puzzle size={26} />
                    <h4>添加第一个 Skill</h4>
                    <p>
                      选择包含 SKILL.md 的目录，也可以将技能放入项目的
                      .xuloom/skills。
                    </p>
                  </div>
                )}
                {session.capabilities.skills.map((skill) => (
                  <article
                    className={
                      "capability-card " +
                      (!skill.valid || skill.shadowed ? "invalid" : "")
                    }
                    key={skill.id}
                  >
                    <div className="card-heading">
                      <Puzzle size={15} />
                      <strong>{skill.name}</strong>
                    </div>
                    <p className="skill-description" title={skill.description || skill.error}>{skill.description || skill.error}</p>
                    <div className="card-meta">
                      <span>
                        {skillScope(skill.scope)}
                      </span>
                      <span className={!skill.valid ? "bad" : ""}>
                        {!skill.valid
                          ? "格式错误"
                          : skill.shadowed
                            ? "被同名技能覆盖"
                            : session.selected.includes(skill.name)
                              ? "已选择"
                              : session.loaded.includes(skill.name)
                                ? "本会话已加载"
                                : ""}
                      </span>
                    </div>
                    {skill.compatibility && (
                      <small className="compatibility">
                        环境要求：{skill.compatibility}
                      </small>
                    )}
                    {skill.valid && skill.enabled && !skill.shadowed && (
                      <div className="card-actions">
                        <button onClick={() => read(skill)}>查看指令</button>
                        <button
                          className={
                            session.selected.includes(skill.name)
                              ? "selected-link"
                              : ""
                          }
                          onClick={() =>
                            update({
                              selected: session.selected.includes(skill.name)
                                ? session.selected.filter(
                                    (name) => name !== skill.name,
                                  )
                                : [...session.selected, skill.name],
                            })
                          }
                        >
                          {session.selected.includes(skill.name)
                            ? "已选择"
                            : "选择使用"}
                        </button>
                      </div>
                    )}
                    {skill.error && skill.description && (
                      <small className="bad">{skill.error}</small>
                    )}
                  </article>
                ))}
                <details className="root-details">
                  <summary>
                    扫描目录与优先级 <ChevronDown size={13} />
                  </summary>
                  <p>
                    项目 .xuloom → .agents → .claude → 导入目录 → 环境目录 →
                    全局目录 → 内置目录。同名技能以首个格式正确的目录为准。
                  </p>
                </details>
              </div>
              <div className="panel-footnote">
                <CircleCheck size={13} />
                名称和描述常驻，完整指令按需加载
              </div>
            </>
          )}
          {tab === "agents" && (
            <>
              <div className="panel-title">
                <div>
                  <h3>子代理</h3>
                  <p>分配独立任务，查看执行结果</p>
                </div>
                <button className="icon-button" aria-label="刷新子代理" onClick={() => refresh().catch(onError)}><RefreshCw size={15} /></button>
              </div>
              <button
                className="import-button"
                onClick={() => importDirectory("agents")}
              >
                <Plus size={15} />
                导入代理定义目录
              </button>
              <button className="capability-guide-link" onClick={() => setGuide("agents")}>Subagent 怎么配置？查看目录和示例</button>
              <div className="panel-scroll">
                {session.workers.length > 0 && (
                  <>
                    <span className="section-label">会话任务</span>
                    {session.workers.map((worker) => (
                      <article
                        className="capability-card"
                        key={worker.agent_id}
                      >
                        <div className="card-heading">
                          <Bot size={15} />
                          <strong>{worker.description || worker.agent_type || "子代理"}</strong>
                          <span className={"status-dot " + worker.status} />
                        </div>
                        <p>{worker.task}</p>
                        <div className="card-meta">
                          <span>{worker.agent_type || "子代理"}{worker.backend === "claude" ? " · Claude Code" : ""} · {worker.model || (worker.executor?.startsWith("ssh:") ? "执行主机默认模型" : "继承主模型")}</span>
                          <span>
                            {worker.outcome_unknown ? "结果待确认" : worker.status === "queued" ? worker.phase === "awaiting_resume" ? "等待恢复队列" : "排队中" : worker.status === "running"
                              ? worker.phase === "starting" ? "启动中" : "执行中"
                              : worker.status === "completed"
                                ? "已完成"
                                : worker.status === "cancelled"
                                  ? "已取消"
                                  : "异常"}
                          </span>
                        </div>
                        {worker.started_at && <div className="card-meta"><span>启动 {new Date(worker.started_at).toLocaleTimeString("zh-CN", { hour12: false })}</span></div>}
                        {!!worker.pending_message_count && <p className="fine-print">{worker.pending_message_count} 条消息待处理{worker.status !== "running" && " · 请在主会话继续此代理"}</p>}
                        {worker.remote_reference && <div className="card-meta" title={worker.remote_reference.cwd}><span>SSH · {worker.remote_reference.machine || worker.remote_reference.host}</span><button className="text-button" onClick={() => onNavigateChat(worker.remote_reference!.url)}>查看远端历史与文件</button></div>}
                        {worker.status === "queued" && worker.phase === "awaiting_resume" && <button className="text-button" disabled={session.busy}
                          onClick={() => invoke("agents.resume_queue", { agentIds: [worker.agent_id] }).then(refresh).catch(onError)}>恢复排队</button>}
                        {(["queued", "running"].includes(worker.status) || worker.outcome_unknown) && (
                          <button
                            className="text-button"
                            onClick={() =>
                              invoke("agents.cancel", {
                                agentId: worker.agent_id,
                              }).catch(onError)
                            }
                          >
                            <Square size={12} />
                            停止代理
                          </button>
                        )}
                        {worker.result && (
                          <details>
                            <summary>查看结果</summary>
                            <div className="worker-result">
                              <Markdown text={worker.result} sessionId={session.id} />
                            </div>
                          </details>
                        )}
                      </article>
                    ))}
                  </>
                )}
                <span className="section-label">可用角色</span>
                {session.capabilities.agents.map((agent, index) => (
                  <article
                    className={
                      "capability-card " + (!agent.valid ? "invalid" : "")
                    }
                    key={agent.name + index}
                  >
                    <div className="card-heading">
                      <Bot size={15} />
                      <strong>{agent.name}</strong>
                      {!agent.valid && <CircleAlert size={14} />}
                    </div>
                    <p>{agent.description || agent.error}</p>
                    <div className="card-meta">
                      <span>
                        {agent.scope === "builtin"
                          ? "内置"
                          : agent.scope === "project"
                            ? "项目"
                            : "自定义"}
                      </span>
                      <span>
                        {!agent.valid
                          ? "不可用"
                          : agent.shadowed
                            ? "被覆盖"
                            : agent.model || "继承模型"}
                      </span>
                    </div>
                    {agent.allowed_tools && (
                      <small className="compatibility">
                        工具：{agent.allowed_tools.join(", ") || "无"}
                      </small>
                    )}
                    {agent.valid && !agent.shadowed && (
                      <div className="card-actions">
                      <button className="text-button" onClick={() => invoke("agents.read", { name: agent.name }).then((text) => setPreview({ name: agent.name, text, kind: "agent" })).catch(onError)}>查看代理定义</button>
                      <button
                        className="text-button"
                        disabled={session.controlledExecution}
                        onClick={() =>
                          update({
                            draft:
                              "请使用 " +
                              agent.name +
                              " 子代理完成以下任务：\n",
                          })
                        }
                      >
                        分配任务 →
                      </button>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </>
          )}
          {tab === "activity" && (
            <ActivityPanel session={session} initialQuery={activityQuery} />
          )}
          {tab === "changes" && <ChangesPanel key={session.id} session={session} invoke={invoke} onOpen={onOpenFile} />}
          {tab === "files" && <WorkspaceTree sessionId={session.id} project={session.project} changes={session.fileChanges} onOpen={onOpenFile} />}
        </>
      )}
      {guide && session && <CapabilityGuide kind={guide} session={session} invoke={invoke} update={update} close={() => setGuide(null)} />}
      {preview && (
        <div className="overlay" onClick={() => setPreview(null)}>
          <section
            className="dialog skill-preview"
            role="dialog"
            aria-label={preview.kind === "agent" ? "代理定义" : "技能指令"}
            onClick={(e) => e.stopPropagation()}
          >
            <header>
              <div>
                <span className="eyebrow">{preview.kind === "agent" ? "SUBAGENT DEFINITION" : "SKILL INSTRUCTIONS"}</span>
                <h2>{preview.name}</h2>
              </div>
              <button
                aria-label={preview.kind === "agent" ? "关闭代理预览" : "关闭技能预览"}
                className="icon-button"
                onClick={() => setPreview(null)}
              >
                <X size={18} />
              </button>
            </header>
            <div className="preview-body">
              <Markdown text={preview.text} />
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
