import { useEffect, useRef, useState } from "react";
import { RefreshCw, X, FolderTree, WrapText } from "lucide-react";
import type { FileChange } from "./types";
import Markdown from "./Markdown";
import FileChanges, { ChangeCounts, relativeFile } from "./FileChanges";
import DiffView from "./DiffView";
import WorkspaceTree from "./WorkspaceTree";
import HtmlPreview from "./HtmlPreview";
import MediaPreview from "./MediaPreview";
import PanelResizer from "./PanelResizer";
import { clampReviewTreeWidth, reviewTreeLimits } from "./panel-width";

export default function FilePreview({ sessionId, project, path, change, version, close, onOpen, initialView, changes, treeWidth, setTreeWidth, saveTreeWidth, line, column, directory = false }: {
  sessionId: string; project: string; path: string; line?: number; column?: number; directory?: boolean; change?: FileChange; version?: number;
  initialView: "diff" | "content"; changes: FileChange[];
  close: () => void; onOpen: (path: string, change?: FileChange) => void;
  treeWidth: number; setTreeWidth: (width: number) => void; saveTreeWidth: (width: number) => void;
}) {
  const layoutRef = useRef<HTMLDivElement>(null);
  const [layoutWidth, setLayoutWidth] = useState(318), [wrap, setWrap] = useState(true);
  useEffect(() => {
    const element = layoutRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setLayoutWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const shownTreeWidth = clampReviewTreeWidth(treeWidth, layoutWidth);
  const [file, setFile] = useState<{ content: string; path: string; size: number; truncated: boolean; mime?: string; version?: string }>();
  const [error, setError] = useState(""), [loading, setLoading] = useState(true), [refresh, setRefresh] = useState(0);
  const [gitChange, setGitChange] = useState<FileChange>(), [diffError, setDiffError] = useState("");
  const [gitFiles, setGitFiles] = useState<FileChange[]>([]);
  const [diffLoading, setDiffLoading] = useState(false), [tree, setTree] = useState(true), [scope, setScope] = useState("workspace");
  const markdown = /\.md(?:own)?$/i.test(path);
  const media = /\.(png|jpe?g|webp|gif|bmp|mp4|webm|m4a|weba|mp3|wav|flac|ogg)$/i.test(path);
  const html = /\.html?$/i.test(path), previewable = markdown || html || media;
  const [tab, setTab] = useState(line ? "source" : media ? "preview" : initialView === "diff" ? "diff" : previewable ? "preview" : "source");
  useEffect(() => {
    if (change?.source !== "git") return;
    let cancelled = false;
    window.xuloom.invoke("changes", { session: sessionId }).then((value) => { if (!cancelled) setGitFiles(value.files || []); }).catch(() => {});
    return () => { cancelled = true; };
  }, [sessionId, change?.source, refresh]);
  useEffect(() => {
    setTab(line ? "source" : media ? "preview" : initialView === "diff" ? "diff" : previewable ? "preview" : "source");
  }, [path, line, column, initialView, change?.updatedAt]);
  useEffect(() => {
    let cancelled = false; setLoading(true); setError(""); setFile(undefined);
    if (directory) { setLoading(false); return; }
    window.xuloom.invoke(media ? "files.mediaInfo" : "files.read", { session: sessionId, path }).then((value) => { if (!cancelled) setFile(media ? { ...value, content: "", truncated: false } : value); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sessionId, path, directory, refresh, version]);
  useEffect(() => {
    let cancelled = false; setGitChange(undefined); setDiffError(""); setDiffLoading(change?.source === "git");
    if (change?.source === "git") window.xuloom.invoke("files.gitDiff", { session: sessionId, path }).then((value) => { if (!cancelled) setGitChange(value); })
      .catch((reason) => { if (!cancelled) setDiffError(String(reason)); }).finally(() => { if (!cancelled) setDiffLoading(false); });
    return () => { cancelled = true; };
  }, [sessionId, path, change, refresh]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); close(); } };
    document.addEventListener("keydown", escape); return () => document.removeEventListener("keydown", escape);
  }, [close]);
  useEffect(() => {
    if (!line || !file || tab !== "source") return;
    const row = layoutRef.current?.querySelector<HTMLElement>(`[data-source-line="${line}"]`);
    row?.scrollIntoView({ block: "center" });
    if (row && column) {
      const code = row.querySelector("code")?.firstChild;
      if (code?.nodeType === Node.TEXT_NODE) {
        const range = document.createRange(), offset = Math.min(column - 1, code.textContent?.length || 0);
        range.setStart(code, offset); range.collapse(true);
        const selection = window.getSelection(); selection?.removeAllRanges(); selection?.addRange(range);
      }
    }
  }, [file, line, column, tab]);
  if (directory) return <section className="file-preview" role="region" aria-label="文件夹预览">
    <header><h2 title={path}>{relativeFile(path, project) || path}</h2><button aria-label="关闭文件预览" className="icon-button" onClick={close}><X size={19} /></button></header>
    <WorkspaceTree sessionId={sessionId} project={path} rootPath={path} onOpen={onOpen} />
  </section>;
  const shownChange = change?.source === "git" ? gitChange : change;
  const listedChanges = change?.source === "git" ? gitFiles : changes;
  return <section className="file-preview docked-review" role="region" aria-label="文件预览">
    <header><div><h2 title={path}>{relativeFile(path, project).split(/[\\/]/).pop()}</h2><small title={path}>{tab === "diff" ? (change?.source === "git" ? "Git 工作区 · HEAD 与当前文件" : "会话变更 · 任务修改前后") : "文件内容 · 磁盘最新版本"}</small></div>
      {!media && <button className="icon-button" aria-label="自动换行" aria-pressed={wrap} title="源码与差异自动换行" onClick={() => setWrap(!wrap)}><WrapText size={16} /></button>}
      <button className="icon-button" aria-label="显示工作区文件树" aria-pressed={tree} onClick={() => setTree(!tree)}><FolderTree size={16} /></button>
      <button className="icon-button" aria-label="刷新文件内容" disabled={loading || diffLoading} onClick={() => setRefresh(refresh + 1)}><RefreshCw size={16} /></button>
      <button className="icon-button" aria-label="关闭文件预览" onClick={close}><X size={19} /></button></header>
    <div className="review-layout" ref={layoutRef}>
      <div className="review-editor">
        <nav className="file-preview-tabs" role="tablist" aria-label="文件视图">
          {[["diff", "差异"], ["source", "源码"], ["preview", "预览"]].filter(([id]) => media ? id === "preview" : id !== "preview" || previewable).map(([id, label]) =>
            <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
          {shownChange && !media && <ChangeCounts added={shownChange.added} removed={shownChange.removed} />}
        </nav>
        <div className={"file-preview-body " + (wrap ? "wrap-lines " : "") + (tab === "preview" && html ? "web-preview-body" : "")} role="tabpanel">
          {tab === "diff" ? <>
            {diffLoading && <p role="status">正在读取 Git 差异…</p>}
            {diffError && <p role="alert" className="error">{diffError}</p>}
            {!diffLoading && !diffError && (shownChange?.statsAvailable ? <>
              {shownChange.truncated && <p className="preview-limitation">差异较大，已截断显示。</p>}
              {shownChange.diff ? <DiffView diff={shownChange.diff} /> : <p className="muted">内容没有行级变化。</p>}
            </> : <p className="muted">{shownChange ? "未保存可用的文本差异，可能是旧记录、二进制或大文件。" : "此文件没有已记录的差异。"}可以切换到源码查看最新内容。</p>)}
          </> : <>
            {loading && <p role="status">正在读取文件…</p>}
            {error && <p role="alert" className="error">{error}</p>}
            {file && !loading && <>
              <p className="preview-limitation">最新文件 · {file.size.toLocaleString()} 字节{file.truncated && !(tab === "preview" && html) ? " · 仅展示前 500 KB" : ""}</p>
              {tab === "preview" && (media ? <MediaPreview sessionId={sessionId} path={file.path} mime={file.mime || ""} revision={`${file.version || ""}:${refresh}`} /> : html ? <HtmlPreview sessionId={sessionId} path={file.path} revision={`${version || 0}:${refresh}`} /> : <div className="markdown"><Markdown text={file.content} onOpenFile={onOpen} sessionId={sessionId} basePath={file.path} /></div>)}
              {tab === "source" && <div className="file-source">{file.content.split(/\r?\n/).map((sourceText, index) => <div className={"source-line " + (line === index + 1 ? "source-selected" : "")} data-source-line={index + 1} aria-current={line === index + 1 ? "location" : undefined} key={index}><span className="line-number">{index + 1}</span><code>{sourceText || " "}</code></div>)}</div>}
            </>}
          </>}
        </div>
      </div>
      {tree && <aside className="review-files" style={{ flexBasis: shownTreeWidth }}>
        <PanelResizer width={shownTreeWidth} viewport={layoutWidth} bounds={reviewTreeLimits(layoutWidth)}
          label="调整文件目录栏宽度" defaultWidth={220} change={setTreeWidth} save={saveTreeWidth} />
        <nav className="review-file-tabs" aria-label="文件列表范围">
        <button aria-pressed={scope === "workspace"} onClick={() => setScope("workspace")}>工作区</button>
        <button aria-pressed={scope === "changes"} onClick={() => setScope("changes")}>变更文件</button>
      </nav>{scope === "workspace" ? <WorkspaceTree {...{ sessionId, project, onOpen }} changes={listedChanges} selected={file?.path || path} />
        : <div className="review-change-list">{listedChanges.length ? <FileChanges files={listedChanges} project={project} onOpen={onOpen} compact /> : <p className="muted">没有文件变更。</p>}</div>}</aside>}
    </div>
  </section>;
}
