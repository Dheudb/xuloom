import { useEffect, useState } from "react";
import type { Capabilities, ModelConnection } from "./types";

export default function ExtensionSettings({ kind, busy, invoke, changed }: {
  kind: "skills" | "agents"; busy: boolean;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  changed: (value: Capabilities) => void;
}) {
  const [data, setData] = useState<Capabilities>(), [pending, setPending] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false), [preview, setPreview] = useState("");
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
    <h3>{kind === "skills" ? "Skills · 可复用工作流程" : "Subagents · 独立任务角色"}</h3>
    <p className="fine-print">{kind === "skills" ? "导入已有 SKILL.md 目录，或直接编写一个新技能。有效技能默认可用，输入 $ 选择。" : "导入 Markdown 定义目录，或新建角色。定义角色不会启动任务；委派沿用会话的权限和协作档位。"}</p>
    <div className="actions"><button disabled={busy || pending} onClick={() => { setCreating(!creating); setPreview(""); }}>新建 {label}</button><button disabled={busy || pending} onClick={() => void action("capabilities.import", { kind })}>导入目录</button><button disabled={pending} onClick={() => void action("capabilities.list")}>刷新列表</button></div>
    {busy && <p className="notice">任务运行中可以查看配置，停止任务后才能新增或导入。</p>}
    {creating && <form className="extension-form" onSubmit={async e => { e.preventDefault(); const value = await action("capabilities.create", { kind, ...form }); if (value) { setCreating(false); setNotice(`${label} 已保存并加载`); } }}>
      <fieldset disabled={busy || pending}>
        <label>保存范围<select aria-label="扩展保存范围" value={form.scope} onChange={e => setForm({ ...form, scope: e.target.value })}><option value="global">全局 · 所有会话可用</option><option value="project">当前工作区</option></select></label>
        <p className="extension-path">{root}</p>
        <label>名称<input aria-label="扩展名称" required maxLength={64} pattern="[a-z0-9]+(-[a-z0-9]+)*" placeholder="my-research" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label>描述与使用时机<input aria-label="扩展描述" required maxLength={1024} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
        {kind === "agents" && <><label>执行位置<select aria-label="代理执行位置" value={form.executor} onChange={e => setForm({ ...form, executor: e.target.value, connection: "", model: "inherit" })}><option value="local">本机 · 当前工作目录</option>{hosts.map(host => <option key={host.id} value={`ssh:${host.id}`}>SSH · {host.name} · {host.host}</option>)}</select></label>
          <label>Agent 后端<select aria-label="代理后端" value={form.backend} onChange={e => setForm({ ...form, backend: e.target.value, backend_program: "" })}><option value="native">Xuloom · 原生引擎</option><option value="claude">Claude Code · 已安装 CLI</option></select></label>
          {form.backend === "claude" && <><label>外部程序路径<input aria-label="代理程序路径" placeholder="留空查找执行主机 PATH 中的 Claude Code" value={form.backend_program} onChange={e => setForm({ ...form, backend_program: e.target.value })} /></label><p className="fine-print">需要执行主机已安装支持受控 MCP 的 Claude Code，以及 Anthropic Messages 协议连接。内置执行工具关闭；文件和命令仍由 Xuloom 授权。不会下载程序或使用该 CLI 的个人登录。</p></>}
          <label>供应商连接<select aria-label="代理连接" disabled={loadingModels} value={form.connection} onChange={e => setForm({ ...form, connection: e.target.value, model: "inherit" })}><option value="">{form.executor === "local" ? "继承主代理连接" : "执行主机的默认连接"}</option>{connections.map(item => <option key={item.connectionId} value={item.connectionId}>{item.connectionName} · {item.provider}</option>)}</select></label>
          {loadingModels && <p role="status" className="fine-print">正在读取执行主机的模型连接…</p>}
          <label>模型{selectedConnection ? <select aria-label="代理模型" value={form.model} onChange={e => setForm({ ...form, model: e.target.value })}><option value="inherit">连接的默认模型 · {selectedConnection.model}</option>{selectedConnection.models.map(model => <option key={model} value={model}>{model}</option>)}</select> : <input aria-label="代理模型" value={form.model} onChange={e => setForm({ ...form, model: e.target.value })} />}</label>
          <p className="fine-print">{form.executor === "local" ? "连接与模型在任务入队时固定；后续可以轮换 Key，不能把已有任务静默改派到另一地址。" : "沿用已配置 SSH 主机的工作目录。模型、Skills 和 MCP 在执行主机解析；本机 Key 和技能文件不会上传。主机目标在入队时固定，模型在远端打开任务时固定。"}</p>
          <label>允许工具（逗号分隔；留空继承）<input aria-label="代理工具" placeholder="Read, Grep, Glob, Skill" value={form.tools} onChange={e => setForm({ ...form, tools: e.target.value })} /></label><label>挂载 Skills（逗号分隔）<input aria-label="代理技能" value={form.skills} onChange={e => setForm({ ...form, skills: e.target.value })} /></label></>}
        <label>执行指令<textarea aria-label="扩展指令" required rows={7} maxLength={100000} value={form.prompt} onChange={e => setForm({ ...form, prompt: e.target.value })} /></label>
        <small>保存在你的配置目录中，不打包进 EXE；不会覆盖已有同名文件。</small>
        <div className="actions"><button type="submit" className="primary">保存 {label}</button><button type="button" onClick={() => setCreating(false)}>取消新建</button></div>
      </fieldset>
    </form>}
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status">{notice}</p>}
    {entries.map((item, index) => <section className="extension-row" key={item.name + index}><strong>{item.name}</strong><small>{item.scope === "builtin" ? "内置" : item.scope === "global" ? "全局" : item.scope === "project" ? "当前工作区" : item.scope === "imported" ? "导入" : item.scope} · {!item.valid ? "格式错误" : item.shadowed ? "被同名配置覆盖" : "可用"}</small><p>{item.description}</p>{item.error && <p className="error">{item.error}</p>}<button disabled={pending || !item.valid || item.shadowed} onClick={async () => { const value = await action(kind === "skills" ? "skills.read" : "agents.read", { name: item.name }); if (value) setPreview(String(value)); }}>查看指令</button></section>)}
    {preview && <pre className="extension-preview">{preview}</pre>}
    <div className="actions"><button disabled={pending} onClick={() => void action("capabilities.directory", { kind, scope: "global" })}>打开全局目录</button><button disabled={pending} onClick={() => void action("capabilities.directory", { kind, scope: "project" })}>打开当前工作区目录</button></div>
  </div>;
}
