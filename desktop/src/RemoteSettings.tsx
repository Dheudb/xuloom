import { useEffect, useState } from "react";
import { Server, Plus } from "lucide-react";
import RemoteReferences, { type ReferenceAction } from "./RemoteReferences";
import { displayName, t, tServer } from "./i18n";

type Profile = { id: string; name: string; host: string; port: number | null; cwd: string; python: string; platform: "posix" | "windows"; ssh_config: string };
const empty = (): Profile => ({ id: "", name: "", host: "", port: null, cwd: "", python: "python3", platform: "posix", ssh_config: "" });

export default function RemoteSettings({ open, navigationAction, configurationOnly = false }: { open: (id: string, sessionId?: string) => Promise<void>; navigationAction: ReferenceAction; configurationOnly?: boolean }) {
  const [rows, setRows] = useState<Profile[]>([]), [form, setForm] = useState<Profile>(empty);
  const [editing, setEditing] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [status, setStatus] = useState("");
  const [history, setHistory] = useState<Record<string, { id: string; title: string; updated: number }[]>>({});
  async function refresh() { const value = await window.xuloom.invoke("remote.profiles"); setRows(value.connections); }
  useEffect(() => { void refresh().catch(e => setError(String(e))); }, []);
  async function action(work: () => Promise<void>) { setBusy(true); setError(""); setStatus(""); try { await work(); } catch (error) { setError(String(error)); } finally { setBusy(false); } }
  return <div className="remote-settings">
    <h3><Server size={17} /> {t("SSH 远程连接")}</h3>
    <p className="fine-print">{t("使用系统 OpenSSH、你的主机配置与 SSH agent。远端需要安装 Xuloom；模型、Skills 和历史使用远端配置。这里不保存密码或私钥，不会上传本机 API Key。")}</p>
    <p className="fine-print">{t("先在终端确认能通过 SSH 登录并核对主机密钥。未信任的主机、需要交互密码或协议不兼容时，测试会明确失败。")}</p>
    <div className="actions"><button disabled={busy} onClick={() => { setForm(empty()); setEditing(true); }}><Plus size={14} /> {t("新增 SSH 连接")}</button></div>
    {rows.map(row => <section className="extension-row" key={row.id}>
      <strong>{row.name}</strong><small><span>{row.host}</span> · {row.platform === "windows" ? "Windows" : "Linux / macOS"}</small><p className="extension-path">{row.cwd}</p>
      <div className="actions"><button disabled={busy} onClick={() => { setForm(row); setEditing(true); }}>{t("编辑连接")}</button><button disabled={busy} onClick={() => void action(async () => { const info = await window.xuloom.invoke("remote.check", { id: row.id }); setStatus(t("连接成功：{hostname} · {cwd} · Xuloom {version}", { hostname: info.hostname, cwd: info.cwd, version: info.version })); })}>{t("测试 SSH 连接")}</button>
        {!configurationOnly && <button disabled={busy} onClick={() => void action(() => open(row.id))}>{t("新建远程会话")}</button>}
        {!configurationOnly && <button disabled={busy} onClick={() => void action(async () => { const value = await window.xuloom.invoke("remote.history", { id: row.id }); setHistory(previous => ({ ...previous, [row.id]: value })); })}>{t("查看远端会话")}</button>}
        <button disabled={busy} onClick={() => void action(async () => { await window.xuloom.invoke("remote.remove", { id: row.id }); await refresh(); if (form.id === row.id) setEditing(false); setStatus(t("SSH 连接配置已删除；远端文件与历史未删除")); })}>{t("删除连接配置")}</button></div>
      {history[row.id] && <div className="remote-history">{history[row.id].length ? history[row.id].map(chat => <button disabled={busy} key={chat.id} onClick={() => void action(() => open(row.id, chat.id))}>{displayName(chat.title) || t("新会话")}</button>) : <p className="fine-print">{t("此远端目录还没有保存的会话。")}</p>}</div>}
    </section>)}
    {editing && <form className="extension-form" onSubmit={event => { event.preventDefault(); void action(async () => { await window.xuloom.invoke("remote.save", form); await refresh(); setEditing(false); setStatus(t("SSH 连接已保存；CLI 可使用相同连接 ID")); }); }}><fieldset disabled={busy}>
      <label>{t("连接 ID")}<input aria-label={t("SSH 连接 ID")} required maxLength={64} pattern="[a-zA-Z0-9][a-zA-Z0-9_-]*" value={form.id} onChange={e => setForm({ ...form, id: e.target.value })} /></label>
      <label>{t("名称")}<input aria-label={t("SSH 连接名称")} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
      <label>{t("SSH 主机或别名")}<input aria-label={t("SSH 主机")} required placeholder={t("my-server 或 user@hostname")} value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} /></label>
      <label>{t("远端系统")}<select aria-label={t("SSH 远端系统")} value={form.platform} onChange={e => setForm({ ...form, platform: e.target.value as Profile["platform"], python: e.target.value === "windows" ? "python" : "python3" })}><option value="posix">Linux / macOS</option><option value="windows">Windows</option></select></label>
      <label>{t("远端工作目录")}<input aria-label={t("SSH 远端目录")} required placeholder={form.platform === "windows" ? "C:\\work\\project" : "/home/user/project"} value={form.cwd} onChange={e => setForm({ ...form, cwd: e.target.value })} /></label>
      <label>{t("远端 Python 路径")}<input aria-label="SSH Python" required placeholder={t("已安装 Xuloom 的 Python")} value={form.python} onChange={e => setForm({ ...form, python: e.target.value })} /></label>
      <details><summary>{t("连接选项")}</summary><label>{t("端口")}<input aria-label={t("SSH 端口")} type="number" min={1} max={65535} placeholder={t("使用 SSH 主机配置")} value={form.port ?? ""} onChange={e => setForm({ ...form, port: e.target.value ? Number(e.target.value) : null })} /></label><small>{t("留空时使用主机别名中的端口；没有配置时使用 22。")}</small>
        <label>{t("本机 SSH 配置文件（可选）")}<input aria-label={t("SSH 配置文件")} placeholder={t("留空使用系统默认配置")} value={form.ssh_config} onChange={e => setForm({ ...form, ssh_config: e.target.value })} /></label></details>
      <div className="actions"><button className="primary" type="submit">{t("保存 SSH 连接")}</button><button type="button" onClick={() => setEditing(false)}>{t("取消编辑")}</button></div>
    </fieldset></form>}
    {busy && <p role="status">{t("处理中…")}</p>}{status && <p role="status">{tServer(status)}</p>}{error && <p role="alert" className="error">{tServer(error)}</p>}
    {!configurationOnly && <RemoteReferences action={navigationAction} />}
  </div>;
}
