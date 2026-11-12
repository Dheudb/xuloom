import { useEffect, useState } from "react";
import { sessionTitle, t, tServer } from "./i18n";

type Reference = { id: string; version: string; title: string; valid: boolean; message: string; host?: string; connection?: string; chatId?: string };
export type ReferenceAction = (method: string, params: Record<string, unknown>) => Promise<void>;

export default function RemoteReferences({ action }: { action: ReferenceAction }) {
  const [rows, setRows] = useState<Reference[]>([]), [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false), [confirm, setConfirm] = useState("");
  const [error, setError] = useState(""), [status, setStatus] = useState("");
  async function refresh() {
    setLoading(true); setError(""); setConfirm("");
    try { setRows((await window.xuloom.invoke("remote.references")).references); }
    catch (error) { setError(String(error)); } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  async function forget(row: Reference) {
    setPending(true); setError(""); setStatus("");
    try {
      await action("remote.forget", { id: row.id, version: row.version, confirm: true });
      setStatus(t("本机引用已移除，原始设置已备份。远端历史与文件未删除。"));
      await refresh();
    } catch (error) { setError(String(error)); setConfirm(""); } finally { setPending(false); }
  }
  return <section className="remote-references" aria-label={t("本机 SSH 导航引用")}>
    <h3>{t("本机 SSH 导航引用")}</h3>
    <p className="fine-print">{t("离线也可清理。移除会同时清除此引用保存的本机草稿，并备份原始设置；不会删除远端历史、文件或 SSH 连接配置。远端任务需先停止。")}</p>
    <button disabled={loading || pending} onClick={() => void refresh()}>{t("刷新本机引用")}</button>
    {loading ? <p role="status">{t("正在读取本机引用…")}</p> : rows.length === 0 ? <p>{t("没有本机 SSH 导航引用。")}</p> : rows.map(row => <section className="extension-row" key={row.id} data-navigation-reference={row.id}>
      <strong>{row.chatId ? sessionTitle(row) : row.title}</strong>{row.host && <small>{row.connection} · {row.host}</small>}
      <p className={row.valid ? "fine-print" : "error"}>{tServer(row.message)}</p>
      {confirm === row.id ? <div className="actions"><button disabled={pending} onClick={() => void forget(row)}>{t("确认移除本机引用")}</button><button disabled={pending} onClick={() => setConfirm("")}>{t("保留引用")}</button></div> :
        <button disabled={pending} onClick={() => { setConfirm(row.id); setError(""); }}>{t("移除本机引用")}</button>}
    </section>)}
    {status && <p role="status">{tServer(status)}</p>}{error && <p role="alert" className="error">{tServer(error)}</p>}
  </section>;
}
