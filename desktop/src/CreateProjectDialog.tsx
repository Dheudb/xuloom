import { useEffect, useRef, useState } from "react";
import { Folder, FolderPlus, LoaderCircle, X } from "lucide-react";
import { t, tServer } from "./i18n";

export default function CreateProjectDialog({ create, close }: {
  create: (name: string, path?: string) => Promise<void>; close: () => void;
}) {
  const [name, setName] = useState(""), [folder, setFolder] = useState("");
  const [choosing, setChoosing] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null), nameEdited = useRef(false);
  const busy = choosing || saving;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    input.current?.focus();
    return () => previous?.focus();
  }, []);
  async function choose() {
    setChoosing(true); setError("");
    try {
      const selected = await window.xuloom.invoke("project.choose");
      if (selected) {
        setFolder(selected);
        if (!nameEdited.current) setName(selected.replace(/[\\/]+$/, "").split(/[\\/]/).pop()?.slice(0, 100) || t("新项目"));
      }
    } catch (e) { setError(String(e)); } finally { setChoosing(false); }
  }
  return <div className="overlay" onMouseDown={e => { if (e.target === e.currentTarget && !busy) close(); }}>
    <form className="dialog create-project-dialog" role="dialog" aria-modal="true" aria-label={t("创建项目")} onSubmit={async e => {
      e.preventDefault(); if (busy || !name.trim()) return;
      setSaving(true); setError("");
      try { await create(name.trim(), folder || undefined); close(); } catch (e) { setError(String(e)); } finally { setSaving(false); }
    }} onKeyDown={e => {
      if (e.key === "Escape") { e.stopPropagation(); if (!busy) close(); }
      if (e.key === "Tab") {
        const items = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled)')];
        if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1)?.focus(); }
        else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0]?.focus(); }
      }
    }}>
      <header><h2>{t("创建项目")}</h2><button type="button" className="icon-button" aria-label={t("关闭创建项目")} disabled={busy} onClick={close}><X size={18} /></button></header>
      <label className="project-name-field"><span><Folder size={18} /></span><input ref={input} aria-label={t("项目名称")} placeholder={t("项目名称")} value={name} maxLength={100} required disabled={busy}
        onChange={e => { nameEdited.current = true; setName(e.target.value); }} /></label>
      <div className="project-source-label">{t("源文件夹")}<span>{t("可选")}</span></div>
      <div className={"project-source " + (folder ? "has-folder" : "")}>
        {folder ? <div className="project-source-selected"><Folder size={22} /><div><strong>{folder.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || folder}</strong><p title={folder}>{folder}</p></div>
          <button type="button" className="icon-button" aria-label={t("移除源文件夹")} disabled={busy} onClick={() => setFolder("")}><X size={16} /></button></div>
          : <p>{t("在此电脑上添加文件夹")}</p>}
        <button className="project-folder-add" type="button" disabled={busy} onClick={() => void choose()}>{choosing ? <LoaderCircle size={16} className="spin" /> : <FolderPlus size={16} />}{folder ? t("更换文件夹") : t("添加文件夹")}</button>
      </div>
      {error && <p className="error" role="alert">{tServer(error)}</p>}
      <footer><small>{folder ? t("使用所选文件夹中的文件。") : t("未添加文件夹时，使用默认工作区中的新项目目录。")}</small><button type="button" className="project-create-cancel" disabled={busy} onClick={close}>{t("取消")}</button>
        <button type="submit" className="project-create-submit" disabled={busy || !name.trim()}>{saving && <LoaderCircle size={14} className="spin" />}{saving ? t("创建中…") : t("创建项目")}</button></footer>
    </form>
  </div>;
}
