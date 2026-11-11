import { useEffect, useRef, useState } from "react";
import type { Capabilities, ModelConnection } from "./types";
import { t, tServer, tx } from "./i18n";

export default function ExtensionSettings({ kind, busy, invoke, changed }: {
  kind: "skills" | "agents"; busy: boolean;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  changed: (value: Capabilities) => void;
}) {
  const [data, setData] = useState<Capabilities>(), [pending, setPending] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [preview, setPreview] = useState<{ key: string; content: string } | null>(null);
  const previewRef = useRef<HTMLPreElement>(null);
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [hosts, setHosts] = useState<{ id: string; name: string; host: string }[]>([]), [loadingModels, setLoadingModels] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", prompt: "", scope: "global", executor: "local", backend: "native", backend_program: "", connection: "", model: "inherit", tools: "", skills: "" });
  const label = kind === "skills" ? "Skill" : "Subagent";
  async function action(method: string, params = {}) {
    setPending(true); setError(""); setNotice("");
    try {
      const value = await invoke(method, params);
      if (value?.skills && value?.agents) { setData(value); changed(value); }
      return value;
    } catch (e) { setError(String(e)); return null; }
    finally { setPending(false); }
  }
  useEffect(() => { void action("capabilities.list"); }, []);
  useEffect(() => { previewRef.current?.scrollIntoView({ block: "nearest" }); }, [preview]);
  useEffect(() => { if (kind === "agents") { let active = true; void invoke("remote.profiles").then(value => { if (active) setHosts(value.connections || []); }).catch(e => { if (active) setError(String(e)); }); return () => { active = false; }; } }, [kind]);
  useEffect(() => { if (kind === "agents") {
    let active = true; setLoadingModels(true); setConnections([]); setError("");
    void invoke(form.executor === "local" ? "config.get" : "remote.models", form.executor === "local" ? {} : { id: form.executor.slice(4) })
      .then(value => { if (active) setConnections(value.connections || []); })
      .catch(e => { if (active) setError(String(e)); }).finally(() => { if (active) setLoadingModels(false); });
    return () => { active = false; };
  } }, [kind, form.executor]);
  const selectedConnection = connections.find(item => item.connectionId === form.connection);
  const entries = data?.[kind] || [];
  const root = data?.settings[kind === "skills" ? form.scope === "global" ? "globalSkillsRoot" : "projectSkillsRoot" : form.scope === "global" ? "globalAgentsRoot" : "projectAgentsRoot"];
  return <div className="extension-settings">
    <h3>{kind === "skills" ? t("Skills · 可复用工作流程") : t("Subagents · 独立任务角色")}</h3>
    <p className="fine-print">{kind === "skills" ? t("导入已有 SKILL.md 目录，或直接编写一个新技能。有效技能默认可用，输入 $ 选择。") : t("导入 Markdown 定义目录，或新建角色。定义角色不会启动任务；委派沿用会话的权限和协作档位。")}</p>
    <div className="actions"><button disabled={busy || pending} onClick={() => { setCreating(!creating); setPreview(null); }}>{tx("新建 {label}", { label })}</button><button disabled={busy || pending} onClick={() => void action("capabilities.import", { kind })}>{t("导入目录")}</button><button disabled={pending} onClick={() => void action("capabilities.list")}>{t("刷新列表")}</button></div>
    {busy && <p className="notice">{t("任务运行中可以查看配置，停止任务后才能新增或导入。")}</p>}
    {creating && <form className="extension-form" onSubmit={async e => { e.preventDefault(); const value = await action("capabilities.create", { kind, ...form }); if (value) { setCreating(false); setNotice(t("{label} 已保存并加载", { label })); } }}>
      <fieldset disabled={busy || pending}>
        <label>{t("保存范围")}<select aria-label={t("扩展保存范围")} value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value })}><option value="global">{t("全局 · 所有会话可用")}</option><option value="project">{t("当前工作区")}</option></select></label>
        <p className="extension-path">{root}</p>
        <label>{t("名称")}<input aria-label={t("扩展名称")} required maxLength={64} pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="my-research" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label>{t("描述与使用时机")}<input aria-label={t("扩展描述")} required maxLength={1024} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
        {kind === "agents" && <><label>{t("执行位置")}<select aria-label={t("代理执行位置")} value={form.executor} onChange={e => setForm({ ...form, executor: e.target.value, connection: "", model: "inherit" })}><option value="local">{t("本机 · 当前工作目录")}</option>{hosts.map(host => <option key={host.id} value={`ssh:${host.id}`}>SSH · {host.name} · {host.host}</option>)}</select></label>
          <label>{t("Agent 后端")}<select aria-label={t("代理后端")} value={form.backend} onChange={e => setForm({ ...form, backend: e.target.value, backend_program: "" })}><option value="native">{t("Xuloom · 原生引擎")}</option><option value="claude">{t("Claude Code · 已安装 CLI")}</option><option value="pi">{t("Pi · 已安装 CLI")}</option><option value="codex">{t("Codex · 已安装 CLI")}</option><option value="dsh">{t("DSH · 已安装 CLI")}</option></select></label>
          {form.backend !== "native" && <><label>{t("外部程序路径")}<input aria-label={t("代理程序路径")} placeholder={form.backend === "pi" ? t("已安装 Pi 的 dist/bundle/cli.js；留空查找 PATH") : form.backend === "dsh" ? t("已安装 DSH 的 lib/bin.js；留空查找 PATH") : t("留空查找执行主机 PATH 中的 {value}", { value: form.backend === "codex" ? "Codex" : "Claude Code" })} value={form.backend_program} onChange={e => setForm({ ...form, backend_program: e.target.value })} /></label><p className="fine-print">{tx("需要执行主机已安装所选 CLI，以及{value}连接。{value2}不会下载程序或使用该 CLI 的个人登录。{value3}", { value: form.backend === "codex" ? " OpenAI Responses API " : ["pi", "dsh"].includes(form.backend) ? t(" Anthropic Messages 或 OpenAI Chat ") : " Anthropic Messages ", value2: form.backend === "codex" ? t("模型与工具调用经受控接口核对；文件和命令仍由 Xuloom 授权。") : t("内置执行工具关闭；文件和命令仍由 Xuloom 授权。"), value3: form.backend === "dsh" && t("当前验证 DSH 0.2.1-rc.5；初始任务接受文本，工具仍可返回图片。") })}</p></>}
          <label>{t("供应商连接")}<select aria-label={t("代理连接")} disabled={loadingModels} value={form.connection} onChange={e => setForm({ ...form, connection: e.target.value, model: "inherit" })}><option value="">{form.executor === "local" ? t("继承主代理连接") : t("执行主机的默认连接")}</option>{connections.map(item => <option key={item.connectionId} value={item.connectionId}>{item.connectionName} · {item.provider}</option>)}</select></label>
          {loadingModels && <p role="status" className="fine-print">{t("正在读取执行主机的模型连接…")}</p>}
          <label>{tx("模型{value}", { value: selectedConnection ? <select aria-label={t("代理模型")} value={form.model} onChange={e => setForm({ ...form, model: e.target.value })}><option value="inherit">{tx("连接的默认模型 · {model}", { model: selectedConnection.model })}</option>{selectedConnection.models.map(model => <option key={model} value={model}>{model}</option>)}</select> : <input aria-label={t("代理模型")} value={form.model} onChange={e => setForm({ ...form, model: e.target.value })} /> })}</label>
          <p className="fine-print">{form.executor === "local" ? t("连接与模型在任务入队时固定；后续可以轮换 Key，不能把已有任务静默改派到另一地址。") : t("沿用已配置 SSH 主机的工作目录。模型、Skills 和 MCP 在执行主机解析；本机 Key 和技能文件不会上传。主机目标在入队时固定，模型在远端打开任务时固定。")}</p>
          <label>{t("允许工具（逗号分隔；留空继承）")}<input aria-label={t("代理工具")} placeholder="Read, Grep, Glob, Skill" value={form.tools} onChange={e => setForm({ ...form, tools: e.target.value })} /></label><label>{t("挂载 Skills（逗号分隔）")}<input aria-label={t("代理技能")} value={form.skills} onChange={e => setForm({ ...form, skills: e.target.value })} /></label></>}
        <label>{t("执行指令")}<textarea aria-label={t("扩展指令")} required rows={7} maxLength={100000} value={form.prompt} onChange={e => setForm({ ...form, prompt: e.target.value })} /></label>
        <small>{t("保存在你的配置目录中，不打包进 EXE；不会覆盖已有同名文件。")}</small>
        <div className="actions"><button type="submit" className="primary">{tx("保存 {label}", { label })}</button><button type="button" onClick={() => setCreating(false)}>{t("取消新建")}</button></div>
      </fieldset>
    </form>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}{notice && <p role="status">{tServer(notice)}</p>}
    {entries.map((item, index) => {
      const id = `extension-${kind}-${index}`;
      const key = JSON.stringify([item.name, item.path, item.scope]);
      const expanded = preview?.key === key;
      return <section className="extension-row" key={item.name + index}>
        <strong>{item.name}</strong><small>{item.scope === "builtin" ? t("内置") : item.scope === "global" ? t("全局") : item.scope === "project" ? t("当前工作区") : item.scope === "imported" ? t("导入") : item.scope} · {!item.valid ? t("格式错误") : item.shadowed ? t("被同名配置覆盖") : t("可用")}</small><p>{item.scope === "builtin" ? tServer(item.description) : item.description}</p>{item.error && <p className="error">{tServer(item.error)}</p>}
        <button disabled={pending || !item.valid || item.shadowed} aria-expanded={expanded} aria-controls={expanded ? id : undefined} onClick={async () => {
          if (expanded) { setPreview(null); return; }
          const value = await action(kind === "skills" ? "skills.read" : "agents.read", { name: item.name });
          if (typeof value === "string") setPreview({ key, content: value });
        }}>{expanded ? t("收起指令") : t("查看指令")}</button>
        {expanded && <pre ref={previewRef} id={id} role="region" aria-label={t("{name} 指令", { name: item.name })} tabIndex={0} className="extension-preview">{preview.content || t("暂无指令内容")}</pre>}
      </section>;
    })}
    <div className="actions"><button disabled={pending} onClick={() => void action("capabilities.directory", { kind, scope: "global" })}>{t("打开全局目录")}</button><button disabled={pending} onClick={() => void action("capabilities.directory", { kind, scope: "project" })}>{t("打开当前工作区目录")}</button></div>
  </div>;
}
