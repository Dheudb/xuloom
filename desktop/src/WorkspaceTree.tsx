import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Folder, File, RefreshCw } from "lucide-react";
import type { FileChange } from "./types";
import { ChangeCounts } from "./FileChanges";
import { t, tServer } from "./i18n";

type Entry = { name: string; path: string; directory: boolean; available: boolean };
function Directory({ sessionId, path, depth, revision, selected, changes, onOpen }: {
  sessionId: string; path: string; depth: number; revision: number; selected?: string;
  changes: FileChange[]; onOpen: (path: string) => void;
}) {
  const [entries, setEntries] = useState<Entry[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set()), [truncated, setTruncated] = useState(false);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError("");
    window.xuloom.invoke("files.list", { session: sessionId, path }).then((value) => {
      if (!cancelled) { setEntries(value.entries); setTruncated(value.truncated); }
    }).catch((reason) => { if (!cancelled) setError(String(reason)); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sessionId, path, revision]);
  const normalize = (value: string) => /^[a-z]:/i.test(value) ? value.replace(/\\/g, "/").toLowerCase() : value;
  return <div role="group">
    {loading && <p className="tree-message" role="status">{t("正在读取目录…")}</p>}
    {error && <p className="tree-message error" role="alert">{tServer(error)}</p>}
    {!loading && !error && !entries.length && <p className="tree-message">{t("空文件夹")}</p>}
    {!loading && !error && entries.map((entry) => {
      const open = expanded.has(entry.path), change = changes.find((item) => normalize(item.path) === normalize(entry.path));
      return <div key={entry.path}>
        <button className={"tree-entry " + (selected && normalize(selected) === normalize(entry.path) ? "selected" : "")}
          style={{ paddingLeft: 10 + depth * 14 }} disabled={!entry.available} title={entry.available ? entry.path : t("此链接指向工作区外")}
           aria-label={(entry.directory ? t("文件夹 ") : t("查看文件 ")) + entry.name} aria-expanded={entry.directory ? open : undefined}
          onClick={() => {
            if (!entry.directory) { onOpen(entry.path); return; }
            setExpanded((previous) => { const next = new Set(previous); if (next.has(entry.path)) next.delete(entry.path); else next.add(entry.path); return next; });
          }}>
          {entry.directory ? <>{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}<Folder size={14} /></> : <File size={14} />}
          <span>{entry.name}</span>{change && <ChangeCounts added={change.added} removed={change.removed} />}
        </button>
        {entry.directory && open && <Directory {...{ sessionId, revision, selected, changes, onOpen }} path={entry.path} depth={depth + 1} />}
      </div>;
    })}
    {truncated && <p className="tree-message">{t("此目录超过 1000 项，仅显示前 1000 项。")}</p>}
  </div>;
}

export default function WorkspaceTree({ sessionId, project, selected, changes = [], onOpen, rootPath = "" }: {
  sessionId: string; project: string; rootPath?: string; selected?: string; changes?: FileChange[]; onOpen: (path: string) => void;
}) {
  const [revision, setRevision] = useState(0);
  return <section className="workspace-tree" aria-label={t("工作区文件")}>
    <header><strong title={project}>{t("工作区文件")}</strong><button className="icon-button" aria-label={t("刷新工作区文件")} onClick={() => setRevision(revision + 1)}><RefreshCw size={14} /></button></header>
    <small className="tree-root" title={project}>{project.split(/[\\/]/).filter(Boolean).pop()}</small>
    <div className="tree-scroll"><Directory key={sessionId + project + rootPath} {...{ sessionId, selected, changes, onOpen }} path={rootPath} depth={0} revision={revision + (changes[0]?.updatedAt || 0)} /></div>
  </section>;
}
