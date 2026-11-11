import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { FileText, Globe, Link2, ListFilter, LoaderCircle, RefreshCw, Search, Wrench, X } from "lucide-react";
import { names } from "./ActivityPanel";
import { toolPresentation } from "./tool-presentation";
import ToolIcon from "./ToolIcon";
import { t, tServer, tx } from "./i18n";

type OutputFile = { path: string; name: string; operation?: string };
type Source = { url: string; title: string; kind: "search" | "page" | "resource" };
type Summary = { scope: "turn" | "conversation"; turnId: string | null; files: OutputFile[]; sources: Source[];
  tools: { name: string; count: number; errors: number }[]; warnings: string[]; legacySourcesUnavailable: boolean };

function address(value: string) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url : null;
  } catch { return null; }
}

export default function SessionSummary({ sessionId, busy, openFile, openActivity }: {
  sessionId: string; busy: boolean; openFile: (path: string) => void; openActivity: (tool?: string) => void;
}) {
  const [open, setOpen] = useState(false), [all, setAll] = useState(false);
  const [value, setValue] = useState<Summary>(), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [revision, setRevision] = useState(0), [limit, setLimit] = useState(12);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLElement>(null);
  const [position, setPosition] = useState({ left: 12, top: 64 });
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (rect) setPosition({ left: Math.max(12, Math.min(window.innerWidth - Math.min(380, window.innerWidth - 32) - 12,
        rect.right - Math.min(380, window.innerWidth - 32))), top: rect.bottom + 8 });
    };
    place(); window.addEventListener("resize", place);
    const observer = new ResizeObserver(place);
    if (root.current?.closest(".topbar")) observer.observe(root.current.closest(".topbar")!);
    if (root.current?.closest(".topbar-actions")) observer.observe(root.current.closest(".topbar-actions")!);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true); setError(""); setValue(undefined);
    window.xuloom.invoke("session.summary", { session: sessionId, all }).then((summary) => {
      if (!cancelled) setValue(summary);
    }).catch((failure) => {
      if (!cancelled) setError(String(failure?.message || failure).includes("Unknown method")
        ? t("当前执行端暂不支持摘要，请升级该机器上的 Xuloom。") : t("无法读取已保存的摘要，请重试。"));
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, all, sessionId, busy, revision]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node) && !popup.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
      }
    };
    let timer: ReturnType<typeof setTimeout>;
    const unsubscribe = window.xuloom.onEvent((event) => {
      if (event.session === sessionId && ["tool_result", "subagent_status", "turn_finished"].includes(event.type)) {
        clearTimeout(timer); timer = setTimeout(() => setRevision(r => r + 1), 200);
      }
    });
    document.addEventListener("pointerdown", outside);
    window.addEventListener("keydown", escape, true);
    return () => {
      clearTimeout(timer); unsubscribe(); document.removeEventListener("pointerdown", outside);
      window.removeEventListener("keydown", escape, true);
    };
  }, [open, sessionId]);
  async function visit(event: MouseEvent<HTMLAnchorElement>, url: string) {
    event.preventDefault();
    try { await window.xuloom.invoke("external.open", { url }); }
    catch { setError(t("无法打开来源链接，请重试。")); }
  }
  const files = value?.files || [], sources = (value?.sources || []).filter(source => address(source.url));
  return <div className="session-summary" ref={root}>
    <button ref={trigger} className="icon-button" aria-label={t("输出与来源")} title={t("输出与来源")}
      aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}><ListFilter size={17} /></button>
    {open && createPortal(<section ref={popup} style={{ top: position.top,
      left: `clamp(12px, ${position.left}px, max(12px, calc(100vw - 392px)))` }}
      className="summary-popover" role="dialog" aria-label={t("输出与来源摘要")}>
      <header><strong>{t("输出与来源")}</strong><div>
        <button className="icon-button" aria-label={t("刷新摘要")} title={t("刷新")} disabled={loading} onClick={() => setRevision(r => r + 1)}><RefreshCw size={14} /></button>
        <button className="icon-button" aria-label={t("关闭摘要")} onClick={() => { setOpen(false); trigger.current?.focus(); }}><X size={15} /></button>
      </div></header>
      <div className="summary-scope" role="group" aria-label={t("摘要范围")}>
        <button aria-pressed={!all} onClick={() => { setAll(false); setLimit(12); }}>{t("本轮")}</button>
        <button aria-pressed={all} onClick={() => { setAll(true); setLimit(12); }}>{t("整个会话")}</button>
      </div>
      <div className="summary-scroll" aria-busy={loading}>
        {loading && <p className="summary-muted"><LoaderCircle size={14} className="spin" />{t("读取保存的记录…")}</p>}
        {error && <p role="alert" className="summary-error">{tServer(error)}</p>}
        {!!files.length && <section aria-label={t("输出文件")}><h3>{t("输出内容")} <span>{files.length}</span></h3>
          {files.slice(0, limit).map(file => <button key={file.path} className="summary-row" title={file.path} onClick={() => { setOpen(false); openFile(file.path); }}>
            <FileText size={16} /><span className="summary-label">{file.name || file.path.split(/[\\/]/).pop()}</span>
          </button>)}
        </section>}
        {!!sources.length && <section aria-label={t("来源链接")}><h3>{t("来源")} <span>{sources.length}</span></h3>
          {sources.slice(0, limit).map(source => {
            const Icon = source.kind === "search" ? Search : source.kind === "page" ? Globe : Link2;
            return <a key={source.url} className="summary-row" href={source.url} target="_blank" rel="noopener noreferrer" title={source.url}
              onClick={event => void visit(event, source.url)} onAuxClick={event => { if (event.button === 1) void visit(event, source.url); }}>
              <Icon size={16} /><span className="summary-label"><span>{source.title || address(source.url)?.hostname}</span>
                <small>{address(source.url)?.hostname} · {source.kind === "search" ? t("搜索结果") : source.kind === "page" ? t("已读取网页") : t("工具返回资源")}</small>
              </span>
            </a>;
          })}
        </section>}
        {!!value?.tools.length && <section aria-label={t("已用工具")}><h3>{t("已用工具")}</h3>
          {value.tools.slice(0, limit).map(tool => <button className="summary-row" key={tool.name} title={tool.name}
            onClick={() => { setOpen(false); openActivity(tool.name); }}>
            <ToolIcon kind={toolPresentation({ name: tool.name, status: "running" }).kind} />
            <span className="summary-label">{names[tool.name] || tool.name}</span><small>{tx("{count} 次{value}", { count: tool.count, value: tool.errors ? t(" · {errors} 失败", { errors: tool.errors }) : "" })}</small>
          </button>)}
        </section>}
        {value && !files.length && !sources.length && <p className="summary-muted">{tx("{value}尚无输出文件或联网来源。", { value: all ? t("此会话") : t("本轮") })}</p>}
        {Math.max(files.length, sources.length, value?.tools.length || 0) > limit && <button className="summary-more" onClick={() => setLimit(n => n + 20)}>{t("显示更多")}</button>}
        {value?.legacySourcesUnavailable && <p className="summary-note">{t("部分旧记录未保存结构化来源，不从回答文字推测补齐。")}</p>}
        {value?.warnings?.map((warning, index) => <p className="summary-note" key={index}>{warning}</p>)}
      </div>
      {!!value?.tools.length && <footer><button className="summary-row" onClick={() => { setOpen(false); openActivity(); }}>
        <Wrench size={15} /><span>{t("查看执行活动")}</span><small>{tx("{tools} 次调用", { tools: value.tools.reduce((n, t) => n + t.count, 0) })}</small>
      </button></footer>}
    </section>, document.body)}
  </div>;
}
