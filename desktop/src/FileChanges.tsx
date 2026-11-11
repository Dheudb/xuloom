import { useState } from "react";
import { ChevronDown, FileDiff, Undo2 } from "lucide-react";
import type { FileChange } from "./types";
import { t, tServer, tx } from "./i18n";

export function relativeFile(path: string, project: string) {
  const value = path.replaceAll("\\", "/"), root = project.replaceAll("\\", "/").replace(/\/$/, "") + "/";
  const windows = /^[a-z]:\//i.test(value);
  return (windows ? value.toLowerCase().startsWith(root.toLowerCase()) : value.startsWith(root)) ? value.slice(root.length) : path;
}
export function ChangeCounts({ added, removed }: { added: number | null; removed: number | null }) {
  return added === null || removed === null ? <small className="file-counts-unknown">{t("未记录行数")}</small>
    : <span className="file-counts"><span className="lines-added">+{added}</span><span className="lines-removed">-{removed}</span></span>;
}
export default function FileChanges({ files, project, onOpen, onUndo, compact = false }: {
  files: FileChange[]; project: string; onOpen: (path: string, change?: FileChange) => void; onUndo?: (file: FileChange) => Promise<void>; compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [confirm, setConfirm] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [restored, setRestored] = useState<string[]>([]);
  if (!files.length) return null;
  const known = files.filter((file) => file.statsAvailable);
  const mediaCount = files.filter(file => !file.statsAvailable && file.operation === "generate").length;
  return <section className={"file-changes " + (compact ? "compact" : "")} aria-label={t("文件变更")}>
    <header><FileDiff size={17} /><div><strong>{compact ? files[0]?.source === "git" ? t("Git 未提交的文件") : t("会话修改的文件") : t("已修改 {length} 个文件", { length: files.length })}</strong>
      {(known.length > 0 || mediaCount !== files.length) && <ChangeCounts added={known.length ? known.reduce((sum, file) => sum + (file.added || 0), 0) : null} removed={known.length ? known.reduce((sum, file) => sum + (file.removed || 0), 0) : null} />}
      {mediaCount > 0 && <small>{tx("{mediaCount} 项媒体产物", { mediaCount })}</small>}
      {files.some(file => !file.statsAvailable && file.operation !== "generate") && <small>{t("部分文件未记录行数")}</small>}</div></header>
    {(expanded || compact ? files : files.slice(0, 3)).map((file) => <div key={file.path} className="file-change-actions"><button className="changed-file-row" title={file.path}
      aria-label={t("打开文件 {value}", { value: relativeFile(file.path, project) })} onClick={() => onOpen(file.path, file)}>
      <span>{relativeFile(file.path, project)}</span>{file.operation === "generate" && !file.statsAvailable ? <small className="file-counts-unknown">{t("媒体产物")}</small> : <ChangeCounts added={file.added} removed={file.removed} />}</button>
      {onUndo && file.revisionId && !restored.includes(file.revisionId) && <button className="icon-button" aria-label={t("撤销最近一次写入 {value}", { value: relativeFile(file.path, project) })} title={t("撤销该文件最近一次工具写入")} disabled={busy}
        onClick={() => { setConfirm(file.revisionId!); setError(""); }}><Undo2 size={14} /></button>}
      {confirm === file.revisionId && <div className="file-undo-confirm"><span>{t("恢复这一次写入前的内容？后续改动会阻止撤销。")}</span><button disabled={busy} onClick={() => setConfirm("")}>{t("取消")}</button><button disabled={busy} onClick={async () => {
        setBusy(true); setError(""); try { await onUndo!(file); setRestored((old) => [...old, file.revisionId!]); setConfirm(""); }
        catch (reason) { setError(String(reason)); } finally { setBusy(false); }
      }}>{t("确认撤销")}</button></div>}
      </div>)}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {!compact && files.length > 3 && <button className="show-more-files" onClick={() => setExpanded(!expanded)}>
      {expanded ? t("收起文件列表") : t("再显示 {value} 个文件", { value: files.length - 3 })}<ChevronDown size={13} /></button>}
  </section>;
}
