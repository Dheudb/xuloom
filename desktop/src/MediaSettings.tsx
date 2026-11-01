import { useEffect, useState } from "react";
import { AudioLines, ImagePlus, Music2, Plus, Trash2, Video } from "lucide-react";

type Kind = "image" | "image-edit" | "video" | "speech" | "sound" | "music";
type Model = { id: string; kind: Kind; model: string; prompt_field: string; image_field: string; defaults: Record<string, unknown> };
type Connection = { id: string; name: string; protocol: "fal-queue"; base_url: string; network_mode: "direct" | "system";
  models: Model[]; download_origins: string[]; hasKey?: boolean };
type Form = Omit<Connection, "models" | "download_origins"> & { apiKey: string; downloads: string; models: (Model & { json: string })[] };
type Job = { id: string; kind: Kind; phase: string; model_id: string; request_id?: string; error?: string; files?: { path: string }[] };
const labels: Record<Kind, string> = { image: "图片", "image-edit": "图片编辑", video: "视频", speech: "语音", sound: "音效", music: "音乐" };
const phases: Record<string, string> = { submitting: "提交结果待核对", submitted: "供应商处理中", unknown: "结果未知", downloaded: "产物已保存",
  failed: "失败", cancelled: "供应商已取消", cancel_requested: "取消尚未确认", result_pending: "结果待获取", result_unavailable: "结果暂不可用", downloading: "下载中", download_failed: "下载失败", unreadable: "记录无法读取" };
const preset = (kind: Kind): Model & { json: string } => ({ id: kind, kind, prompt_field: ["speech", "sound"].includes(kind) ? "text" : "prompt", image_field: "image_urls",
  model: ({ image: "fal-ai/flux/schnell", "image-edit": "fal-ai/flux-2/edit", speech: "fal-ai/minimax/speech-02-hd", music: "fal-ai/minimax-music/v2.6" } as Partial<Record<Kind, string>>)[kind] || "",
  defaults: {}, json: kind === "speech" ? '{\n  "output_format": "url"\n}' : kind === "music" ? '{\n  "is_instrumental": true\n}' : "{}" });
const fresh = (): Form => ({ id: "", name: "", protocol: "fal-queue", base_url: "https://queue.fal.run", network_mode: "direct", apiKey: "", downloads: "", models: [] });

export default function MediaSettings({ invoke, busy = false, hasSession, control }: {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>; busy?: boolean; hasSession: boolean;
  control?: (action: "status" | "cancel", identity: string) => Promise<void>;
}) {
  const [rows, setRows] = useState<Connection[]>([]), [form, setForm] = useState<Form>(fresh);
  const [editing, setEditing] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState(""), [status, setStatus] = useState("");
  const [deleteId, setDeleteId] = useState(""), [jobs, setJobs] = useState<Job[]>([]);
  const [originalId, setOriginalId] = useState("");
  const [loading, setLoading] = useState(true);
  async function refresh() {
    setLoading(true);
    try {
      const value = await invoke("media.connections"); setRows(value.connections);
      if (hasSession) setJobs((await invoke("media.jobs")).jobs);
    } finally { setLoading(false); }
  }
  useEffect(() => { void refresh().catch(error => setError(String(error))); }, []);
  async function action(work: () => Promise<void>) {
    setSaving(true); setError(""); setStatus("");
    try { await work(); } catch (error) { setError(String(error)); } finally { setSaving(false); }
  }
  function edit(row: Connection) {
    setOriginalId(row.id);
    setForm({ ...row, apiKey: "", downloads: row.download_origins.join("\n"), models: row.models.map(model => ({ ...model, json: JSON.stringify(model.defaults, null, 2) })) });
    setEditing(true); setError(""); setStatus(""); setDeleteId("");
  }
  function changeModel(index: number, patch: Partial<Form["models"][number]>) { setForm(previous => ({ ...previous, models: previous.models.map((row, at) => at === index ? { ...row, ...patch } : row) })); }
  return <div className="media-settings" aria-busy={loading}>
    <h3><ImagePlus size={17} /> 媒体生成</h3>
    <p className="fine-print">为图片、视频、语音、音效和音乐配置生成服务。一个连接共用一个 Key，可添加多个模型。当前支持 fal Queue API 及实现同一协议的服务；不会把聊天 API 当作生成接口。</p>
    <p className="fine-print">保存不会调用服务。生成时使用当前会话权限，费用由所选供应商计收；本地停止等待不代表供应商已取消。</p>
    {error && <p role="alert" className="error">{error}</p>}{status && <p role="status" className="notice">{status}</p>}
    {loading && <p role="status" className="fine-print">正在读取媒体配置与任务记录…</p>}
    {!editing && <>
      {rows.map(row => <article className="media-connection" key={row.id}>
        <header><strong>{row.name}</strong><span>{row.hasKey ? "Key 已保存" : "未填写 Key"}</span></header>
        <p title={row.base_url}>{row.base_url}</p>
        <div className="media-kind-list">{row.models.length ? row.models.map(model => <span key={model.id} title={model.model}>{labels[model.kind]} · {model.id}</span>) : <span>尚未设置模型</span>}</div>
        <footer><button disabled={saving || busy} onClick={() => edit(row)}>编辑连接</button><button disabled={saving || busy} onClick={() => {
          if (deleteId !== row.id) { setDeleteId(row.id); return; }
          void action(async () => { const value = await invoke("media.remove", { id: row.id, confirm: true }); setRows(value.connections); setDeleteId(""); setStatus("连接已移除；供应商已有任务没有被取消。"); });
        }}>{deleteId === row.id ? "确认移除连接" : "移除"}</button>{deleteId === row.id && <button onClick={() => setDeleteId("")}>取消移除</button>}</footer>
        {deleteId === row.id && <p className="fine-print">已有任务需要原连接才能继续查询；移除不会取消远端任务。</p>}
      </article>)}
      <button disabled={saving || busy || loading} onClick={() => { setForm(fresh()); setOriginalId(""); setEditing(true); setStatus(""); }}><Plus size={15} /> 添加生成服务</button>
    </>}
    {editing && <form onSubmit={event => { event.preventDefault(); void action(async () => {
      if (!originalId && rows.some(row => row.id === form.id)) throw new Error("这个连接 ID 已存在，请使用新的 ID 或编辑原连接。");
      const models = form.models.map(({ json, ...model }) => ({ ...model, model: model.model.trim(), defaults: JSON.parse(json || "{}") }));
      const value = await invoke("media.save", { connection: { id: form.id, name: form.name, protocol: form.protocol, base_url: form.base_url.trim(),
        network_mode: form.network_mode, apiKey: form.apiKey, models, download_origins: form.downloads.split("\n").map(value => value.trim()).filter(Boolean) } });
      setRows(value.connections); setForm(fresh()); setEditing(false); setStatus("媒体连接已保存。可直接在对话中描述要生成的内容。");
    }); }}>
      <fieldset disabled={saving || busy}>
        <div className="media-form-pair"><label>连接 ID<input aria-label="媒体连接 ID" value={form.id} disabled={!!originalId} onChange={event => setForm({ ...form, id: event.target.value })} placeholder="my-media" required /></label>
          <label>显示名称<input aria-label="媒体连接名称" value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} placeholder="我的生成服务" /></label></div>
        <label>协议<select aria-label="媒体协议" value={form.protocol} disabled><option value="fal-queue">fal Queue API</option></select></label>
        <label>API 地址<input aria-label="媒体 API 地址" value={form.base_url} onChange={event => setForm({ ...form, base_url: event.target.value })} required /></label>
        <label>API Key<input aria-label="媒体 API Key" type="password" autoComplete="off" spellCheck={false} value={form.apiKey} onChange={event => setForm({ ...form, apiKey: event.target.value })} placeholder={form.hasKey ? "已保存；留空保留" : "填写供应商的 Key"} /></label>
        <label>连接方式<select aria-label="媒体连接方式" value={form.network_mode} onChange={event => setForm({ ...form, network_mode: event.target.value as "direct" | "system" })}><option value="direct">直接连接</option><option value="system">使用系统代理</option></select></label>
        <h4>模型</h4><p className="fine-print">模型 ID 是本地选择名称；供应商端点填写完整 owner/model/endpoint。视频和音效请填写当前可用模型，避免使用已经下线的端点。</p>
        {form.models.map((model, index) => <article className="media-model" key={index}>
          <header><strong>{labels[model.kind]}</strong><button type="button" className="icon-button" aria-label={"移除媒体模型 " + (model.id || index + 1)} onClick={() => setForm({ ...form, models: form.models.filter((_, at) => at !== index) })}><Trash2 size={14} /></button></header>
          <div className="media-form-pair"><label>模型 ID<input required aria-label={"媒体模型 ID " + (index + 1)} value={model.id} onChange={event => changeModel(index, { id: event.target.value })} /></label>
            <label>用途<select aria-label={"媒体用途 " + (index + 1)} value={model.kind} onChange={event => { const kind = event.target.value as Kind; changeModel(index, { kind, prompt_field: ["speech", "sound"].includes(kind) ? "text" : "prompt" }); }}>{Object.entries(labels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label></div>
          <label>供应商模型端点<input required aria-label={"供应商模型端点 " + (index + 1)} value={model.model} onChange={event => changeModel(index, { model: event.target.value })} placeholder="owner/model/endpoint" /></label>
          <details><summary>模型参数</summary><div className="media-form-pair"><label>提示词字段<input aria-label={"媒体提示词字段 " + (index + 1)} value={model.prompt_field} onChange={event => changeModel(index, { prompt_field: event.target.value })} /></label>
            <label>参考图字段<input aria-label={"媒体图片字段 " + (index + 1)} value={model.image_field} onChange={event => changeModel(index, { image_field: event.target.value })} /></label></div>
            <label>默认参数 JSON<textarea aria-label={"媒体默认参数 " + (index + 1)} rows={4} spellCheck={false} value={model.json} onChange={event => changeModel(index, { json: event.target.value })} /></label>
          </details>
        </article>)}
        <div className="media-add-models">{(Object.keys(labels) as Kind[]).map(kind => <button key={kind} type="button" onClick={() => {
          const next = preset(kind); let id = kind as string; for (let number = 2; form.models.some(row => row.id === id); number++) id = kind + "-" + number;
          setForm({ ...form, models: [...form.models, { ...next, id }] });
        }}>{kind === "video" ? <Video size={13} /> : kind === "music" ? <Music2 size={13} /> : ["sound", "speech"].includes(kind) ? <AudioLines size={13} /> : <ImagePlus size={13} />}{labels[kind]}</button>)}</div>
        <details><summary>产物下载来源</summary><p className="fine-print">默认允许当前 API 来源、fal.media 子域及 storage.googleapis.com。自建服务使用其他 CDN 时，每行填写一个完整来源地址；下载不会附带 API Key。</p>
          <label>额外下载来源<textarea aria-label="媒体下载来源" rows={3} value={form.downloads} onChange={event => setForm({ ...form, downloads: event.target.value })} placeholder="https://cdn.example.org" /></label></details>
        <div className="media-form-actions"><button type="button" onClick={() => { setForm(fresh()); setEditing(false); }}>取消</button><button type="submit">{saving ? "保存中…" : "保存媒体连接"}</button></div>
      </fieldset>
    </form>}
    {hasSession && <section className="media-job-history"><header><h4>此会话已保存的生成任务</h4><button disabled={saving} onClick={() => void action(refresh)}>刷新记录</button></header>
      <p className="fine-print">这里只读取保存的回执，不自动查询供应商。查询/取消会关闭设置并沿用会话权限执行，不调用聊天模型。</p>
      {jobs.length ? jobs.map(job => <details key={job.id}><summary>{labels[job.kind] || "任务"} · {job.model_id || job.id} <span>{phases[job.phase] || job.phase}</span></summary><code>{job.id}</code>{job.request_id && <p className="fine-print">供应商任务：{job.request_id}</p>}{job.error && <p className="fine-print">{job.error}</p>}{job.files?.map(file => <p key={file.path} className="media-job-file">{file.path}</p>)}
        {control && !["downloaded", "failed", "cancelled", "unreadable"].includes(job.phase) && <div className="media-add-models">
          <button disabled={saving || busy} onClick={() => void action(() => control("status", job.id))}>查询供应商</button>
          <button disabled={saving || busy} onClick={() => void action(() => control("cancel", job.id))}>请求取消</button>
        </div>}
      </details>) : !loading && !error && <p className="fine-print">此会话还没有生成任务。</p>}
    </section>}
  </div>;
}
