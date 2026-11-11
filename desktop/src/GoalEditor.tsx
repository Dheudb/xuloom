import { useState } from "react";
import { t, tServer } from "./i18n";

export default function GoalEditor({ objective, invoke, close }: { objective: string; invoke: (method: string, params?: Record<string, unknown>) => Promise<any>; close: () => void }) {
  const [text, setText] = useState(objective), [pending, setPending] = useState(false), [error, setError] = useState("");
  return <form className="goal-editor" onSubmit={async e => {
    e.preventDefault(); setPending(true); setError("");
    try { await invoke("goal.update", { objective: text, resume: true }); close(); }
    catch(e) { setError(String(e)); } finally { setPending(false); }
  }}>
    <label>{t("持续目标")}<textarea aria-label={t("持续目标内容")} rows={6} value={text} disabled={pending} onChange={e => setText(e.target.value)} required /></label>
    <p>{t("保存时先停止当前执行，再沿用此会话和已有结果推进新目标。历史与文件保留，不自动重放已完成的操作。")}</p>
    {error && <p role="alert">{tServer(error)}</p>}
    <footer><button type="button" disabled={pending} onClick={close}>{t("取消修改")}</button><button className="primary" type="submit" disabled={pending || !text.trim()}>{pending ? t("保存中…") : t("保存并继续")}</button></footer>
  </form>;
}
