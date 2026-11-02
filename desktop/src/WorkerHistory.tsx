import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bot, Copy, Image as ImageIcon, RefreshCw, X } from "lucide-react";
import Markdown from "./Markdown";
import ToolIcon from "./ToolIcon";
import { toolPresentation } from "./tool-presentation";

type Entry = { id: string; role: string; kind: string; text: string; name?: string; callId?: string;
  error?: boolean; continued?: boolean; continues?: boolean; imageIndex?: number };
type Page = { version: string; backend: string; entries: Entry[]; nextCursor: string | null; notice: string; live: boolean };
type Invoke = (method: string, params?: Record<string, unknown>) => Promise<any>;
function toolView(entry: Entry) {
  let input = {};
  try { input = JSON.parse(entry.text); } catch { /* Incomplete pages remain readable as text. */ }
  return toolPresentation({ name: (entry.name || "").replace(/^mcp__xuloom__/, ""), status: "completed", input });
}

export default function WorkerHistory({ agentId, name, invoke, close }: { agentId: string; name: string; invoke: Invoke; close: () => void }) {
  const [page, setPage] = useState<Page>(), [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [images, setImages] = useState<Record<string, string>>({});
  const dialog = useRef<HTMLElement>(null), alive = useRef(true), loading = useRef(false);
  async function load(reset = false) {
    if (loading.current) return;
    loading.current = true; setBusy(true); setError("");
    try {
      const value: Page = await invoke("session.transcript", { agentId, cursor: reset ? null : page?.nextCursor });
      if (!alive.current) return;
      setPage(value);
      if (reset) setImages({});
      setEntries(previous => {
        const result = reset ? [] : [...previous];
        for (const entry of value.entries) {
          const last = result[result.length - 1];
          if (entry.continued && last?.id.split(":")[0] === entry.id.split(":")[0])
            result[result.length - 1] = { ...last, text: last.text + entry.text, continues: entry.continues };
          else result.push(entry);
        }
        return result;
      });
    } catch (reason) { if (alive.current) setError(String(reason)); }
    finally { loading.current = false; if (alive.current) setBusy(false); }
  }
  useEffect(() => {
    alive.current = true;
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    void load(true);
    return () => { alive.current = false; previous?.focus(); };
  }, []);
  async function showImage(entry: Entry) {
    try {
      const result = await invoke("session.transcriptImage", { agentId, imageIndex: entry.imageIndex, version: page?.version });
      if (alive.current) setImages(previous => ({ ...previous, [entry.id]: result.source }));
    } catch (reason) { if (alive.current) setError(String(reason)); }
  }
  return createPortal(<div className="overlay worker-history-overlay" onClick={close}>
    <section ref={dialog} className="dialog worker-history" role="dialog" aria-modal="true" aria-label="子代理完整历史"
      onClick={event => event.stopPropagation()} onKeyDown={event => {
        if (event.key === "Escape") { event.stopPropagation(); close(); }
        if (event.key === "Tab") {
          const nodes = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], summary, [tabindex="0"]')];
          const first = nodes[0], last = nodes[nodes.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      }}>
      <header><div><span className="eyebrow">只读执行记录{page ? " · " + page.backend : ""}</span><h2>{name}</h2></div>
        <div className="worker-history-actions"><button className="icon-button" aria-label="刷新历史快照" disabled={busy} onClick={() => void load(true)}><RefreshCw size={17} /></button>
          <button className="icon-button" aria-label="关闭子代理历史" onClick={close}><X size={18} /></button></div></header>
      <div className="worker-history-body" aria-busy={busy}>
        {page && <p className="fine-print">{page.notice}{page.live && " 此代理仍在运行，刷新可查看新记录。"}</p>}
        {entries.map(entry => <article className={"history-entry history-" + entry.kind} key={entry.id}>
          <div className="history-entry-label">{entry.kind === "tool_call" ? <ToolIcon kind={toolView(entry).kind} /> : entry.kind === "image" ? <ImageIcon size={14} /> : <Bot size={14} />}
            <span>{entry.kind === "tool_call" ? entry.name : entry.kind === "tool_result" ? (entry.error ? "工具返回 · 失败" : "工具返回") : entry.kind === "thinking" ? "推理摘要" : entry.role === "user" ? "任务／输入" : "子代理"}</span>
            {!!entry.text && <button className="icon-button" aria-label="复制此条历史" onClick={() => invoke("clipboard.writeText", { text: entry.text }).catch(reason => setError(String(reason)))}><Copy size={13} /></button>}</div>
          {["tool_call", "tool_result", "thinking"].includes(entry.kind)
            ? <details><summary>{entry.kind === "thinking" ? "查看模型公开的推理" : "查看完整内容"}{entry.callId && <code>{entry.callId}</code>}</summary><pre>{entry.text}</pre></details>
            : entry.kind === "image" ? images[entry.id] ? <img className="history-image" alt="子代理历史中的图像" src={images[entry.id]} /> : <button className="text-button" onClick={() => void showImage(entry)}>查看此条记录的图像</button>
            : entry.kind === "notice" ? <p className="fine-print">{entry.text}</p> : <Markdown text={entry.text} />}
          {entry.continues && <p className="fine-print">此条内容尚未加载完，请继续加载；原始记录没有截断。</p>}
        </article>)}
        {!busy && page && !entries.length && <p>尚无已保存的对话内容。</p>}
        {error && <p className="worker-history-error" role="alert">{error} 可刷新快照重试；不会恢复或重放任务。</p>}
        <div className="worker-history-footer">{busy ? <span role="status">正在读取已保存的记录…</span> : page?.nextCursor ? <button onClick={() => void load()}>继续加载历史</button> : page && <span>已到当前快照末尾</span>}</div>
      </div>
    </section>
  </div>, document.body);
}
