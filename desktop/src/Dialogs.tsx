import { useEffect, useRef, useState } from "react";
import { X, KeyRound, Check, FolderOpen, Archive, Puzzle, Bot, Plug, Link2, Server } from "lucide-react";
import type { EngineEvent, ModelConfig, SessionCatalog } from "./types";
import Markdown from "./Markdown";
import ArchivePanel from "./ArchivePanel";
import ExtensionSettings from "./ExtensionSettings";
import RemoteSettings from "./RemoteSettings";
import type { Capabilities } from "./types";
import { ReasoningSettings } from "./ReasoningControls";

export function SettingsDialog({
  config,
  close,
  save,
  defaultDirectory,
  chooseDirectory,
  saveDirectory,
  catalog,
  workspaceAction,
  discoverModels,
  deleteConnection,
  extensions,
}: {
  config?: ModelConfig;
  close: () => void;
  save: (value: ModelConfig & { apiKey: string }) => Promise<void>;
  defaultDirectory: string;
  chooseDirectory: () => Promise<string | null>;
  saveDirectory: (value: string) => Promise<string>;
  catalog: SessionCatalog;
  workspaceAction: (method: string, params: Record<string, unknown>) => Promise<void>;
  discoverModels: (value: Record<string, unknown>) => Promise<{ models: string[] }>;
  deleteConnection: (connectionId: string) => Promise<ModelConfig>;
  extensions?: { busy: boolean; invoke: (method: string, params?: Record<string, unknown>) => Promise<any>; changed: (value: Capabilities) => void; openMcp: () => void };
}) {
  const [tab, setTab] = useState("model");
  const dialogRef = useRef<HTMLElement>(null);
  const [directory, setDirectory] = useState(defaultDirectory);
  const [directoryBusy, setDirectoryBusy] = useState(false);
  const [directorySaved, setDirectorySaved] = useState(false);
  const [chatUrl, setChatUrl] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  const [linkStatus, setLinkStatus] = useState("");
  const [form, setForm] = useState({
    connectionId: config?.connectionId || "legacy-openai",
    connectionName: config?.connectionName || "我的 API",
    provider: config?.provider || "openai",
    model: config?.model || "",
    baseUrl: config?.baseUrl || "",
    hasKey: !!config?.hasKey,
    apiKey: "",
    networkMode: config?.networkMode || "direct",
    modelInputs: config?.modelInputs || {} as NonNullable<ModelConfig["modelInputs"]>,
    modelReasoning: config?.modelReasoning || {} as NonNullable<ModelConfig["modelReasoning"]>,
  });
  const [modelsText, setModelsText] = useState((config?.models || (config?.model ? [config.model] : [])).join("\n"));
  const [discovering, setDiscovering] = useState(false);
  const [discoveryStatus, setDiscoveryStatus] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteId, setDeleteId] = useState("");
  const savedConnection = config?.connections?.find((item) => item.connectionId === form.connectionId);
  const [error, setError] = useState("");
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLButtonElement>("[role=tab]")?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) { event.preventDefault(); event.stopPropagation(); close(); }
    };
    window.addEventListener("keydown", escape, true);
    return () => { window.removeEventListener("keydown", escape, true); previous?.focus(); };
  }, []);
  async function submit() {
    setSaving(true);
    setError("");
    try {
      const models = [...new Set([...modelsText.split(/[\n,，]/).map((name) => name.trim()).filter(Boolean), form.model.trim()])];
      await save({ ...form, models, modelInputs: Object.fromEntries(Object.entries(form.modelInputs).filter(([name]) => models.includes(name))),
        modelReasoning: Object.fromEntries(Object.entries(form.modelReasoning).filter(([name]) => models.includes(name))) });
      close();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="overlay" onClick={close}>
      <section
        role="dialog"
        aria-modal="true"
        aria-label="工作台设置"
        className="dialog settings-dialog"
        ref={dialogRef}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const buttons = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)") || [])
            .filter((element) => element.tabIndex >= 0 && !!element.getClientRects().length);
          const first = buttons[0], last = buttons[buttons.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <div>
            <span className="eyebrow">WORKBENCH SETTINGS</span>
            <h2>设置</h2>
          </div>
          <button className="icon-button" aria-label="关闭设置" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <div className="settings-layout">
          <nav role="tablist" aria-label="设置分类" className="settings-nav" aria-orientation="vertical"
            onKeyDown={(event) => {
              if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=tab]"));
              const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
              buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length]?.click();
            }}>
            {[{ id: "model", label: "模型连接", icon: KeyRound }, { id: "skills", label: "Skills", icon: Puzzle }, { id: "agents", label: "Subagents", icon: Bot }, { id: "mcp", label: "MCP 服务", icon: Plug }, { id: "remote", label: "SSH 远程", icon: Server }, { id: "workspace", label: "工作区", icon: FolderOpen }, { id: "archives", label: "已归档会话", icon: Archive }].map(({ id, label, icon: Icon }) =>
              <button key={id} role="tab" id={"settings-tab-" + id} aria-selected={tab === id} aria-controls={"settings-panel-" + id} tabIndex={tab === id ? 0 : -1}
                onClick={(event) => { setTab(id); setError(""); event.currentTarget.focus(); }}><Icon size={16} />{label}</button>)}
          </nav>
          <div className="settings-content">
        {tab === "remote" && <div role="tabpanel" id="settings-panel-remote" aria-labelledby="settings-tab-remote"><RemoteSettings /></div>}
        {["skills", "agents", "mcp"].includes(tab) && <div role="tabpanel" id={"settings-panel-" + tab} aria-labelledby={"settings-tab-" + tab}>
          {!extensions ? <p>请先新建或打开一个会话，再管理扩展；无需配置模型或打开代码仓库。</p> : tab === "mcp" ? <><h3>MCP 服务</h3><p>连接外部工具。支持本地 stdio 与远程 Streamable HTTP，可浏览市场、自定义服务、测试连接及启停。</p><p>配置保存在当前工作区的 .xuloom/mcp_servers.json；API Key 与本地运行环境由服务提供方决定。</p><button onClick={extensions.openMcp}>打开 MCP 管理</button></> : <ExtensionSettings key={tab} kind={tab as "skills" | "agents"} {...extensions} />}
        </div>}
        <div className="form-section" role="tabpanel" id="settings-panel-model" aria-labelledby="settings-tab-model" hidden={tab !== "model"}>
          <fieldset disabled={saving || discovering || deleting}>
          <h3>
            <KeyRound size={16} /> 模型连接
          </h3>
          <p className="notice">保存模型连接后，新建会话即可使用。</p>
          <div className="api-connection-row"><label>API 连接
            <select aria-label="API 连接" disabled={discovering || saving} value={form.connectionId} onChange={(event) => {
              const connection = config?.connections?.find((item) => item.connectionId === event.target.value);
              if (connection) { setForm({ ...connection, apiKey: "", networkMode: connection.networkMode || "direct", modelInputs: connection.modelInputs || {}, modelReasoning: connection.modelReasoning || {} }); setModelsText(connection.models.join("\n")); setDiscoveryStatus(""); setError(""); setDeleteId(""); }
            }}>
              {(config?.connections || []).map((connection) => <option key={connection.connectionId} value={connection.connectionId}>{connection.connectionName}</option>)}
              {!config?.connections?.some((connection) => connection.connectionId === form.connectionId) && <option value={form.connectionId}>{form.connectionName}</option>}
            </select></label>
            <button disabled={discovering || saving} onClick={() => {
              setForm({ connectionId: crypto.randomUUID(), connectionName: "新 API", provider: "openai", model: "", baseUrl: "", hasKey: false, apiKey: "", networkMode: "direct", modelInputs: {}, modelReasoning: {} });
              setModelsText(""); setDiscoveryStatus(""); setError(""); setDeleteId("");
            }}>新增 API</button>
            <button className="danger" disabled={!savedConnection || saving || discovering || deleting} onClick={() => { setDeleteId(form.connectionId); setError(""); }}>删除 API</button>
          </div>
          {deleteId === savedConnection?.connectionId && <div className="api-delete-confirm" role="group" aria-label="确认删除 API 连接">
            <p>删除「{savedConnection.connectionName}」及其 {savedConnection.models.length} 个模型配置？保存的 Key 也会删除，聊天记录会保留。</p>
            <div className="actions"><button onClick={() => setDeleteId("")}>保留 API</button>
              <button className="danger" onClick={async () => {
                setDeleting(true); setError("");
                try {
                  const next = await deleteConnection(deleteId);
                  setForm({ connectionId: next.connectionId || crypto.randomUUID(), connectionName: next.connectionName || "我的 API", provider: next.provider,
                    model: next.model, baseUrl: next.baseUrl, hasKey: next.hasKey, apiKey: "", networkMode: next.networkMode || "direct", modelInputs: next.modelInputs || {}, modelReasoning: next.modelReasoning || {} });
                  setModelsText((next.models || []).join("\n")); setDeleteId("");
                  setDiscoveryStatus(next.connections?.length ? "已删除 API 连接。" : "已删除所有 API 连接，请添加新的连接。");
                } catch (e) { setError(String(e)); } finally { setDeleting(false); }
              }}>{deleting ? "删除中…" : "确认删除 API"}</button>
            </div>
          </div>}
          <label>连接名称<input aria-label="连接名称" value={form.connectionName} onChange={(event) => setForm({ ...form, connectionName: event.target.value })} /></label>
          <label>
            API 协议
            <select
              aria-label="API 协议"
              value={form.provider}
              onChange={(e) =>
                { setForm({
                  ...form,
                  provider: e.target.value,
                  model: "",
                  baseUrl: "",
                  hasKey: false,
                  apiKey: "",
                  modelInputs: {},
                  modelReasoning: {},
                }); setModelsText(""); setDiscoveryStatus(""); }
              }
            >
              <option value="openai">OpenAI / 兼容 API / 本地模型</option>
              <option value="anthropic">Anthropic</option>
            </select>
          </label>
          <label>可选模型（每行一个 ID）
            <textarea aria-label="可选模型列表" rows={5} value={modelsText} placeholder="model-a\nmodel-b\nmodel-c" onChange={(event) => setModelsText(event.target.value)} />
          </label>
          <div className="model-discovery-actions"><button disabled={discovering || saving} onClick={async () => {
            setDiscovering(true); setDiscoveryStatus(""); setError("");
            try {
              const value = await discoverModels(form);
              const models = [...new Set([...modelsText.split(/[\n,，]/).map((name) => name.trim()).filter(Boolean), ...value.models])];
              setModelsText(models.join("\n"));
              if (!form.model && models.length) setForm((previous) => ({ ...previous, model: models[0] }));
              setDiscoveryStatus(`已读取 ${value.models.length} 个模型；点击保存配置后可切换。`);
            } catch (e) { setError(String(e)); } finally { setDiscovering(false); }
          }}>{discovering ? "正在获取…" : "从 API 获取模型"}</button>
          {discoveryStatus && <span role="status">{discoveryStatus}</span>}</div>
          <p className="fine-print">同一 API 的这些模型共用地址和 Key。当前模型会自动加入列表；可用列表和免费额度由提供方决定。</p>
          <label>
            模型 ID
            <input
              aria-label="模型 ID"
              placeholder="输入供应商提供的完整模型 ID"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
            />
          </label>
          <label>
            当前模型的输入能力
            <select aria-label="模型输入能力" value={!form.modelInputs[form.model] ? "unknown" : form.modelInputs[form.model].includes("document") ? "document" : form.modelInputs[form.model].includes("image") ? "image" : "text"}
              onChange={(event) => {
                const modelInputs = { ...form.modelInputs };
                if (event.target.value === "unknown") delete modelInputs[form.model];
                else modelInputs[form.model] = event.target.value === "document" ? ["text", "image", "document"] : event.target.value === "image" ? ["text", "image"] : ["text"];
                setForm({ ...form, modelInputs });
              }} disabled={!form.model.trim()}>
              <option value="unknown">未声明（交给接口判断）</option><option value="text">仅文本</option>
              <option value="image">文本与图片</option>{form.provider === "anthropic" && <option value="document">文本、图片与原生文档</option>}
            </select>
          </label>
          <p className="fine-print">按当前模型 ID 分别保存。仅文本模型会拦截图片附件和历史图片；未知能力不根据模型名称猜测，也不自动切换模型。</p>
          {!!form.model.trim() && <ReasoningSettings provider={form.provider} profile={form.modelReasoning[form.model]} change={profile => {
            const modelReasoning = { ...form.modelReasoning };
            if (profile) modelReasoning[form.model] = profile; else delete modelReasoning[form.model];
            setForm({ ...form, modelReasoning });
          }} />}
          <label>
            API 地址
            <input
              aria-label="API 地址"
              placeholder="API 根地址或完整的 /chat/completions 地址"
              value={form.baseUrl}
              onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
            />
          </label>
          <label>
            API Key{" "}
            <span className="muted">
              {form.hasKey
                ? "✓ 已保存；输入新 Key 可替换"
                : "本地无鉴权服务可填 not-needed"}
            </span>
            <input
              aria-label="API Key"
              type="password"
              autoComplete="off"
              placeholder={form.hasKey ? "••••••••••••••••（已保存）" : "输入 API Key"}
              value={form.apiKey}
              onChange={(e) => setForm({ ...form, apiKey: e.target.value })}
            />
          </label>
          <label>
            连接方式
            <select aria-label="连接方式" value={form.networkMode}
              onChange={(e) => setForm({ ...form, networkMode: e.target.value as "direct" | "system" })}>
              <option value="direct">直连（本地模型和兼容 API 默认）</option>
              <option value="system">系统代理 / 环境代理</option>
            </select>
          </label>
          <p className="fine-print">
            沿用 Xuloom 本机配置；密钥不会返回聊天界面。
          </p>
        {error && tab === "model" && <p role="alert" className="error">{error}</p>}
        <footer>
          <button onClick={close}>取消</button>
          <button className="primary" disabled={saving || discovering || deleting || !!deleteId || !form.model.trim()} onClick={submit}><Check size={15} />{saving ? "保存中…" : "保存配置"}</button>
        </footer>
          </fieldset>
        </div>
        <div className="form-section" role="tabpanel" id="settings-panel-workspace" aria-labelledby="settings-tab-workspace" hidden={tab !== "workspace"}>
          <h3><Link2 size={16} /> 聊天链接</h3>
          <p className="fine-print">右键会话可复制本机聊天链接。链接只定位已有历史，不包含聊天内容；另一台电脑需要自己的历史记录。</p>
          <label>打开聊天链接<input aria-label="聊天链接" placeholder="xuloom://chat/v1/…" value={chatUrl} onChange={event => setChatUrl(event.target.value)} /></label>
          <div className="actions">
            <button disabled={linkBusy || !chatUrl.trim()} onClick={async () => {
              setLinkBusy(true); setError("");
              try { await workspaceAction("chat.open", { url: chatUrl.trim() }); close(); }
              catch (error) { setError(String(error)); } finally { setLinkBusy(false); }
            }}>打开会话</button>
            <button disabled={linkBusy} onClick={async () => {
              setLinkBusy(true); setError(""); setLinkStatus("");
              try {
                const result = await window.xuloom.invoke("chat.register");
                setLinkStatus(result.registered ? "系统聊天链接已注册" : "系统未确认默认处理程序，请检查桌面环境的默认应用设置");
              } catch (error) { setError(String(error)); } finally { setLinkBusy(false); }
            }}>注册系统链接</button>
          </div>
          {linkStatus && <p role="status" className="fine-print">{linkStatus}</p>}
          <h3><FolderOpen size={16} /> 独立会话工作区根目录</h3>
          <p className="fine-print">每个新建的独立会话会在此目录下分配专属文件夹，互不共用工作文件。聊天记录集中存储；修改此路径不移动已有文件或历史。</p>
          <label>目录路径
            <input aria-label="默认工作目录" value={directory} onChange={(e) => { setDirectory(e.target.value); setDirectorySaved(false); }} />
          </label>
          <div className="actions">
            <button disabled={directoryBusy} onClick={async () => {
              setDirectoryBusy(true); setError("");
              try { const value = await chooseDirectory(); if (value) { setDirectory(value); setDirectorySaved(false); } }
              catch (e) { setError(String(e)); } finally { setDirectoryBusy(false); }
            }}>选择文件夹</button>
            <button disabled={directoryBusy || !directory.trim()} onClick={async () => {
              setDirectoryBusy(true); setError(""); setDirectorySaved(false);
              try { setDirectory(await saveDirectory(directory)); setDirectorySaved(true); }
              catch (e) { setError(String(e)); } finally { setDirectoryBusy(false); }
            }}>{directoryBusy ? "处理中…" : "保存默认目录"}</button>
            {directorySaved && <span role="status">默认目录已保存</span>}
          </div>
          {error && tab === "workspace" && <p role="alert" className="error">{error}</p>}
        </div>
        {tab === "archives" && <div role="tabpanel" id="settings-panel-archives" aria-labelledby="settings-tab-archives"><ArchivePanel catalog={catalog} action={workspaceAction} /></div>}
          </div>
        </div>
      </section>
    </div>
  );
}

export function InteractionCard({
  interaction,
  respond,
}: {
  interaction: EngineEvent;
  respond: (id: string, answer: unknown) => Promise<void>;
}) {
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(answer: unknown) {
    setBusy(true);
    setError("");
    try {
      await respond(interaction.requestId, answer);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }
  return (
    <section className="interaction" aria-label="待处理请求">
      <span className="eyebrow">需要你的输入</span>
      {interaction.type === "permission" && (
        <>
          <h3>允许执行 {interaction.request.tool_name}？</h3>
          <pre>{interaction.request.summary}</pre>
          <div className="actions">
            <button disabled={busy} onClick={() => submit("deny")}>
              拒绝
            </button>
            <button disabled={busy} onClick={() => submit("always")}>
              本会话允许此工具
            </button>
            <button
              disabled={busy}
              className="primary"
              onClick={() => submit("allow")}
            >
              允许一次
            </button>
          </div>
        </>
      )}
      {interaction.type === "plan_approval" && (
        <>
          <h3>审阅执行计划</h3>
          <div className="plan-content">
            <Markdown text={interaction.content} />
          </div>
          <div className="actions">
            <button disabled={busy} onClick={() => submit("deny")}>
              继续修改
            </button>
            <button
              disabled={busy}
              className="primary"
              onClick={() => submit("allow")}
            >
              批准并执行
            </button>
          </div>
        </>
      )}
      {interaction.type === "questions" && (
        <>
          <h3>补充任务信息</h3>
          {interaction.questions.map((q: any) => (
            <div key={q.question} className="question">
              <p>{q.question}</p>
              <div className="choices">
                {q.options.map((option: any) => {
                  const selected = q.multiSelect
                    ? (answers[q.question] || []).includes(option.label)
                    : answers[q.question] === option.label;
                  return (
                    <button
                      key={option.label}
                      className={selected ? "chosen" : ""}
                      onClick={() =>
                        setAnswers((previous) => {
                          const current = previous[q.question];
                          return {
                            ...previous,
                            [q.question]: q.multiSelect
                              ? Array.isArray(current) &&
                                current.includes(option.label)
                                ? current.filter((v) => v !== option.label)
                                : [
                                    ...(Array.isArray(current) ? current : []),
                                    option.label,
                                  ]
                              : option.label,
                          };
                        })
                      }
                    >
                      {option.label}
                      <small>{option.description}</small>
                    </button>
                  );
                })}
              </div>
              <input
                placeholder="或者输入你的回答"
                onChange={(e) =>
                  setAnswers({ ...answers, [q.question]: e.target.value })
                }
              />
            </div>
          ))}
          <div className="actions">
            <button
              disabled={
                busy ||
                interaction.questions.some(
                  (q: any) => !answers[q.question]?.length,
                )
              }
              className="primary"
              onClick={() => submit(answers)}
            >
              提交回答
            </button>
          </div>
        </>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
