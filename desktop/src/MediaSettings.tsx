import { useEffect, useState } from "react";
import { AudioLines, ImagePlus, Music2, Plus, Video } from "lucide-react";

type Kind = "image" | "image-edit" | "video" | "speech" | "sound" | "music";
type Model = { connectionId: string; id: string; kind: Kind; model: string; base_url: string; hasKey: boolean };
type Draft = { connectionId?: string; id?: string; kind: Kind; model: string; base_url: string; apiKey: string; hasKey?: boolean };
type Job = { id: string; kind: Kind; phase: string; model_id: string; request_id?: string; error?: string; files?: { path: string }[] };
const labels: Record<Kind, string> = { image: "图片", "image-edit": "图片编辑", video: "视频", speech: "语音", sound: "音效", music: "音乐" };
const categories = [
  { kind: "image", icon: ImagePlus, hint: "生成图片，也可在对话中附参考图进行编辑。" },
  { kind: "video", icon: Video, hint: "生成视频，任务进度和产物保存在当前会话。" },
  { kind: "speech", icon: AudioLines, hint: "文字转语音、旁白和配音。" },
  { kind: "sound", icon: AudioLines, hint: "环境声、动作声和其他音效。" },
  { kind: "music", icon: Music2, hint: "配乐、歌曲和纯音乐。" },
] as const;
const phases: Record<string, string> = { submitting: "提交结果待核对", submitted: "供应商处理中", unknown: "结果未知", downloaded: "产物已保存",
  failed: "失败", cancelled: "供应商已取消", cancel_requested: "取消尚未确认", result_pending: "结果待获取", result_unavailable: "结果暂不可用", downloading: "下载中", download_failed: "下载失败", unreadable: "记录无法读取" };

export default function MediaSettings({ invoke, busy = false, hasSession, control }: {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>; busy?: boolean; hasSession: boolean;
  control?: (action: "status" | "cancel", identity: string) => Promise<void>;
}) {
  const [rows, setRows] = useState<Model[]>([]), [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false), [loading, setLoading] = useState(true);
  const [error, setError] = useState(""), [status, setStatus] = useState("");
  const [deleting, setDeleting] = useState(""), [jobs, setJobs] = useState<Job[]>([]);
  async function refresh() {
    setLoading(true);
    try {
      setRows((await invoke("media.models")).models);
      if (hasSession) setJobs((await invoke("media.jobs")).jobs);
    } finally { setLoading(false); }
  }
  useEffect(() => { void refresh().catch(error => setError(String(error))); }, []);
  async function action(work: () => Promise<void>) {
    setSaving(true); setError(""); setStatus("");
    try { await work(); } catch (error) { setError(String(error)); } finally { setSaving(false); }
  }
  function edit(row: Model) { setDraft({ ...row, apiKey: "" }); setDeleting(""); setError(""); setStatus(""); }
  return <div className="media-settings" aria-busy={loading}>
    <h3>生成模型</h3>
    <p className="fine-print">选择用途，填写 API 地址、Key 和模型名，然后直接在对话里描述你想生成的内容。</p>
    <p className="fine-print">保存不会发起生成。接口格式和参数由框架处理；未适配的接口会明确提示，不会重复试投任务。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {status && <p role="status" className="notice">{status}</p>}
    {loading && <p role="status" className="fine-print">正在读取生成模型…</p>}
    {categories.map(({ kind, icon: Icon, hint }) => <section className="generation-category" key={kind} aria-label={labels[kind] + "生成模型"}>
      <header><div><h4><Icon size={17} />{labels[kind]}生成模型</h4><p className="fine-print">{hint}</p></div>
        <button type="button" disabled={busy || saving || loading} aria-label={"添加" + labels[kind] + "模型"} onClick={() => {
          setDraft({ kind, base_url: "", apiKey: "", model: "" }); setError(""); setStatus("");
        }}><Plus size={14} />添加模型</button></header>
      {rows.filter(row => row.kind === kind || kind === "image" && row.kind === "image-edit").map(row => {
        const identity = row.connectionId + "/" + row.id;
        return <article className="generation-model-row" key={identity}>
          <div><strong>{row.model}</strong><p title={row.base_url}>{row.base_url}</p><small>{row.hasKey ? "Key 已保存" : "未填写 Key"}{row.kind === "image-edit" ? " · 图片编辑" : ""}</small></div>
          <div className="generation-row-actions"><button disabled={busy || saving} onClick={() => edit(row)}>编辑</button>
            <button disabled={busy || saving} onClick={() => {
              if (deleting !== identity) { setDeleting(identity); return; }
              void action(async () => { setRows((await invoke("media.removeModel", { connectionId: row.connectionId, modelId: row.id, confirm: true })).models); setDeleting(""); setStatus("模型已移除；供应商已有任务没有被取消。"); });
            }}>{deleting === identity ? "确认移除" : "移除"}</button>
            {deleting === identity && <button onClick={() => setDeleting("")}>取消</button>}</div>
        </article>;
      })}
      {draft && (draft.kind === kind || kind === "image" && draft.kind === "image-edit") && <form className="generation-model-form" onSubmit={event => {
        event.preventDefault(); void action(async () => {
          const { hasKey: _, ...value } = draft;
          setRows((await invoke("media.saveModel", { model: { ...value, model: value.model.trim(), base_url: value.base_url.trim() } })).models);
          setDraft(null); setStatus("生成模型已保存，可直接在对话中使用。");
        });
      }}><fieldset disabled={saving || busy}>
        <legend>{draft.id ? "编辑" : "添加"}{labels[draft.kind]}模型</legend>
        <label>API 地址<input aria-label="生成模型 API 地址" type="url" required value={draft.base_url} placeholder="填写 API 根地址或完整生成接口地址" onChange={event => setDraft({ ...draft, base_url: event.target.value })} /></label>
        <label>API Key<input aria-label="生成模型 API Key" type="password" required={!draft.hasKey} autoComplete="off" spellCheck={false} value={draft.apiKey} placeholder={draft.hasKey ? "已保存；留空保留" : "填写你的 API Key"} onChange={event => setDraft({ ...draft, apiKey: event.target.value })} /></label>
        <label>模型名<input aria-label="生成模型名称" required spellCheck={false} value={draft.model} placeholder="填写服务商提供的模型名称" onChange={event => setDraft({ ...draft, model: event.target.value })} /></label>
        <div className="media-form-actions"><button type="button" onClick={() => setDraft(null)}>取消</button><button type="submit">{saving ? "保存中…" : "保存模型"}</button></div>
      </fieldset></form>}
    </section>)}
    <p className="fine-print">生成费用由你填写的供应商收取。停止本地等待不等于供应商已取消任务。</p>
    {hasSession && <section className="media-job-history"><header><h4>此会话已保存的生成任务</h4><button disabled={saving} onClick={() => void action(refresh)}>刷新记录</button></header>
      <p className="fine-print">这里只读取保存的回执，不自动查询供应商。查询／取消沿用会话权限，不调用聊天模型。</p>
      {jobs.length ? jobs.map(job => <details key={job.id}><summary>{labels[job.kind] || "任务"} · {phases[job.phase] || job.phase}</summary><code>{job.id}</code>
        {job.request_id && <p className="fine-print">供应商任务：{job.request_id}</p>}{job.error && <p className="fine-print">{job.error}</p>}
        {job.files?.map(file => <p key={file.path} className="media-job-file">{file.path}</p>)}
        {control && !["downloaded", "failed", "cancelled", "unreadable"].includes(job.phase) && <div className="media-add-models">
          <button disabled={saving || busy} onClick={() => void action(() => control("status", job.id))}>查询供应商</button>
          <button disabled={saving || busy} onClick={() => void action(() => control("cancel", job.id))}>请求取消</button>
        </div>}
      </details>) : !loading && !error && <p className="fine-print">此会话还没有生成任务。</p>}
    </section>}
  </div>;
}
