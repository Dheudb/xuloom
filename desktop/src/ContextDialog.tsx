import { useEffect, useState } from "react";
import { X, LoaderCircle } from "lucide-react";
import { useDialogFocus } from "./useDialogFocus";
type Snapshot = { estimatedTokens: number; window: number; reserve: number; source: string; estimator: string;
  components: { system: number; tools: number; conversation: number; memoryCharacters: number };
  last?: { before: number; after: number; elapsedSeconds: number; backend: string };
  auxiliary: Record<string, { calls: number; input_tokens: number; output_tokens: number }> };
export default function ContextDialog({ invoke, busy, close }: { invoke: (method: string, params?: any) => Promise<any>; busy: boolean; close: () => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [focus, setFocus] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useDialogFocus('[aria-label="添加工具与模式"]');
  useEffect(() => { let live = true; invoke("context.get").then(value => { if (live) setSnapshot(value); }).catch(e => { if (live) setError(String(e)); }); return () => { live = false; }; }, []);
  async function compact() {
    setWorking(true); setError("");
    try { setSnapshot(await invoke("context.compact", { focus })); } catch (e) { setError(String(e)); } finally { setWorking(false); }
  }
  return <div className="overlay" onMouseDown={e => { if (e.target === e.currentTarget && !working) close(); }}>
    <section ref={dialogRef} className="dialog context-dialog" role="dialog" aria-modal="true" aria-label="上下文用量" onKeyDown={e => { if (e.key === "Escape" && !working) close(); }}>
      <header><h2>上下文</h2><button aria-label="关闭上下文" disabled={working} onClick={close}><X size={18} /></button></header>
      {snapshot ? <>
        <p className="context-total">约 {snapshot.estimatedTokens.toLocaleString()} <span>/ {snapshot.window.toLocaleString()} tokens</span></p>
        <progress aria-label="上下文占用比例" max={snapshot.window} value={Math.min(snapshot.estimatedTokens, snapshot.window)} />
        <dl><div><dt>系统与指令（含记忆）</dt><dd>{snapshot.components.system.toLocaleString()}</dd></div><div><dt>工具定义</dt><dd>{snapshot.components.tools.toLocaleString()}</dd></div><div><dt>对话估算</dt><dd>{snapshot.components.conversation.toLocaleString()}</dd></div><div><dt>输出预留</dt><dd>{snapshot.reserve.toLocaleString()}</dd></div></dl>
        <p className="fine-print">{snapshot.source === "conservative_default" ? "模型窗口未声明，当前采用保守值；可在模型的更多设置中填写。" : "按当前模型配置估算。"}实际用量以供应商回执为准。</p>
        {snapshot.last && <p className="fine-print">上次压缩：{snapshot.last.before.toLocaleString()} → {snapshot.last.after.toLocaleString()} tokens · {snapshot.last.elapsedSeconds} 秒</p>}
        {Object.entries(snapshot.auxiliary).map(([name, value]) => <p className="fine-print" key={name}>{({ compaction: "压缩", native_compaction: "原生压缩", session_memory: "会话摘要", long_term_memory: "长期记忆" } as Record<string, string>)[name] || name}：{value.calls} 次 · 输入 {value.input_tokens.toLocaleString()} / 输出 {value.output_tokens.toLocaleString()} tokens</p>)}
      </> : !error && <p>正在读取…</p>}
      <label>压缩时重点保留（可选）<input aria-label="压缩保留重点" value={focus} maxLength={4000} placeholder="例如：当前目标、实验参数和未完成事项" onChange={e => setFocus(e.target.value)} disabled={working} /></label>
      {error && <p role="alert" className="error">{error}</p>}
      <footer><span className="fine-print">压缩会调用已配置的模型；原文会归档。</span>{working ? <button onClick={() => void invoke("stop").catch(e => setError(String(e)))}><LoaderCircle size={14} className="spin" />停止压缩</button> : <button className="primary" disabled={busy || !snapshot} onClick={() => void compact()}>压缩上下文</button>}</footer>
    </section>
  </div>;
}
