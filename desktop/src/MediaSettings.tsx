import { useEffect, useState } from "react";
import { AudioLines, ImagePlus, Music2, Plus, Video } from "lucide-react";
import { t, tServer, tx } from "./i18n";

type Kind = "image" | "image-edit" | "video" | "speech" | "sound" | "music";
type Model = { connectionId: string; id: string; kind: Kind; model: string; base_url: string; hasKey: boolean; credentialUnavailable?: boolean };
type Draft = { connectionId?: string; id?: string; kind: Kind; model: string; base_url: string; apiKey: string; hasKey?: boolean };
type Job = { id: string; kind: Kind; phase: string; model_id: string; request_id?: string; error?: string; files?: { path: string }[] };
const labels: Record<Kind, string> = { image: t("图片"), "image-edit": t("图片编辑"), video: t("视频"), speech: t("语音"), sound: t("音效"), music: t("音乐") };
const categories = [
  { kind: "image", icon: ImagePlus, hint: t("生成图片，也可在对话中附参考图进行编辑。") },
  { kind: "video", icon: Video, hint: t("生成视频，任务进度和产物保存在当前会话。") },
  { kind: "speech", icon: AudioLines, hint: t("文字转语音、旁白和配音。") },
  { kind: "sound", icon: AudioLines, hint: t("环境声、动作声和其他音效。") },
  { kind: "music", icon: Music2, hint: t("配乐、歌曲和纯音乐。") },
] as const;
const phases: Record<string, string> = { submitting: t("提交结果待核对"), submitted: t("供应商处理中"), unknown: t("结果未知"), downloaded: t("产物已保存"),
  failed: t("失败"), cancelled: t("供应商已取消"), cancel_requested: t("取消尚未确认"), result_pending: t("结果待获取"), result_unavailable: t("结果暂不可用"), downloading: t("下载中"), download_failed: t("下载失败"), unreadable: t("记录无法读取") };

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
  function edit(row: Model) { const { connectionId, id, kind, model, base_url, hasKey } = row; setDraft({ connectionId, id, kind, model, base_url, hasKey, apiKey: "" }); setDeleting(""); setError(""); setStatus(""); }
  return <div className="media-settings" aria-busy={loading}>
    <h3>{t("生成模型")}</h3>
    <p className="fine-print">{t("选择用途，填写 API 地址、Key 和模型名，然后直接在对话里描述你想生成的内容。")}</p>
    <p className="fine-print">{t("保存不会发起生成。接口格式和参数由框架处理；未适配的接口会明确提示，不会重复试投任务。")}</p>
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {status && <p role="status" className="notice">{tServer(status)}</p>}
    {loading && <p role="status" className="fine-print">{t("正在读取生成模型…")}</p>}
    {categories.map(({ kind, icon: Icon, hint }) => <section className="generation-category" key={kind} aria-label={t("{value}生成模型", { value: labels[kind] })}>
      <header><div><h4><Icon size={17} />{tx("{value}生成模型", { value: labels[kind] })}</h4><p className="fine-print">{hint}</p></div>
        <button type="button" disabled={busy || saving || loading} aria-label={t("添加{value}模型", { value: labels[kind] })} onClick={() => {
          setDraft({ kind, base_url: "", apiKey: "", model: "" }); setError(""); setStatus("");
        }}><Plus size={14} />{t("添加模型")}</button></header>
      {rows.filter(row => row.kind === kind || kind === "image" && row.kind === "image-edit").map(row => {
        const identity = row.connectionId + "/" + row.id;
        return <article className="generation-model-row" key={identity}>
          <div><strong>{row.model}</strong><p title={row.base_url}>{row.base_url}</p><small>{row.credentialUnavailable ? t("凭据不可用，请编辑并重新填写") : row.hasKey ? t("Key 已保存") : t("未填写 Key")}{row.kind === "image-edit" ? t(" · 图片编辑") : ""}</small></div>
          <div className="generation-row-actions"><button disabled={busy || saving} onClick={() => edit(row)}>{t("编辑")}</button>
            <button disabled={busy || saving} onClick={() => {
              if (deleting !== identity) { setDeleting(identity); return; }
              void action(async () => { setRows((await invoke("media.removeModel", { connectionId: row.connectionId, modelId: row.id, confirm: true })).models); setDeleting(""); setStatus(t("模型已移除；供应商已有任务没有被取消。")); });
            }}>{deleting === identity ? t("确认移除") : t("移除")}</button>
            {deleting === identity && <button onClick={() => setDeleting("")}>{t("取消")}</button>}</div>
        </article>;
      })}
      {draft && (draft.kind === kind || kind === "image" && draft.kind === "image-edit") && <form className="generation-model-form" onSubmit={event => {
        event.preventDefault(); void action(async () => {
          const { hasKey: _, ...value } = draft;
          setRows((await invoke("media.saveModel", { model: { ...value, model: value.model.trim(), base_url: value.base_url.trim() } })).models);
          setDraft(null); setStatus(t("生成模型已保存，可直接在对话中使用。"));
        });
      }}><fieldset disabled={saving || busy}>
        <legend>{tx("{value}{value2}模型", { value: draft.id ? t("编辑") : t("添加"), value2: labels[draft.kind] })}</legend>
        <label>{t("API 地址")}<input aria-label={t("生成模型 API 地址")} type="url" required value={draft.base_url} placeholder={t("填写 API 根地址或完整生成接口地址")} onChange={event => setDraft({ ...draft, base_url: event.target.value })} /></label>
        <label>API Key<input aria-label={t("生成模型 API Key")} type="password" required={!draft.hasKey} autoComplete="off" spellCheck={false} value={draft.apiKey} placeholder={draft.hasKey ? t("已保存；留空保留") : t("填写你的 API Key")} onChange={event => setDraft({ ...draft, apiKey: event.target.value })} /></label>
        <label>{t("模型名")}<input aria-label={t("生成模型名称")} required spellCheck={false} value={draft.model} placeholder={t("填写服务商提供的模型名称")} onChange={event => setDraft({ ...draft, model: event.target.value })} /></label>
        <div className="media-form-actions"><button type="button" onClick={() => setDraft(null)}>{t("取消")}</button><button type="submit">{saving ? t("保存中…") : t("保存模型")}</button></div>
      </fieldset></form>}
    </section>)}
    <p className="fine-print">{t("生成费用由你填写的供应商收取。停止本地等待不等于供应商已取消任务。")}</p>
    {hasSession && <section className="media-job-history"><header><h4>{t("此会话已保存的生成任务")}</h4><button disabled={saving} onClick={() => void action(refresh)}>{t("刷新记录")}</button></header>
      <p className="fine-print">{t("这里只读取保存的回执，不自动查询供应商。查询／取消沿用会话权限，不调用聊天模型。")}</p>
      {jobs.length ? jobs.map(job => <details key={job.id}><summary>{labels[job.kind] || t("任务")} · {phases[job.phase] || job.phase}</summary><code>{job.id}</code>
        {job.request_id && <p className="fine-print">{tx("供应商任务：{request_id}", { request_id: job.request_id })}</p>}{job.error && <p className="fine-print">{tServer(job.error)}</p>}
        {job.files?.map(file => <p key={file.path} className="media-job-file">{file.path}</p>)}
        {control && !["downloaded", "failed", "cancelled", "unreadable"].includes(job.phase) && <div className="media-add-models">
          <button disabled={saving || busy} onClick={() => void action(() => control("status", job.id))}>{t("查询供应商")}</button>
          <button disabled={saving || busy} onClick={() => void action(() => control("cancel", job.id))}>{t("请求取消")}</button>
        </div>}
      </details>) : !loading && !error && <p className="fine-print">{t("此会话还没有生成任务。")}</p>}
    </section>}
  </div>;
}
