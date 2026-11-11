import { useEffect, useState } from "react";
import { X, LoaderCircle } from "lucide-react";
import { useDialogFocus } from "./useDialogFocus";
import { t, tServer, tx } from "./i18n";
type Snapshot = { estimatedTokens: number; window: number; reserve: number; source: string; estimator: string;
  components: { system: number; tools: number; conversation: number; memoryCharacters: number };
  last?: { before: number; after: number; elapsedSeconds: number; backend: string };
  auxiliary: Record<string, { calls: number; input_tokens: number; output_tokens: number; cost_usd?: number }>;
  cost?: { usd: number; unpriced_requests: number; limitUsd: number | null } };
export default function ContextDialog({ invoke, busy, close }: { invoke: (method: string, params?: any) => Promise<any>; busy: boolean; close: () => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [focus, setFocus] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useDialogFocus(t("[aria-label=\"添加工具与模式\"]"));
  useEffect(() => { let live = true; invoke("context.get").then(value => { if (live) setSnapshot(value); }).catch(e => { if (live) setError(String(e)); }); return () => { live = false; }; }, []);
  async function compact() {
    setWorking(true); setError("");
    try { setSnapshot(await invoke("context.compact", { focus })); } catch (e) { setError(String(e)); } finally { setWorking(false); }
  }
  return <div className="overlay" onMouseDown={e => { if (e.target === e.currentTarget && !working) close(); }}>
    <section ref={dialogRef} className="dialog context-dialog" role="dialog" aria-modal="true" aria-label={t("上下文用量")} onKeyDown={e => { if (e.key === "Escape" && !working) close(); }}>
      <header><h2>{t("上下文")}</h2><button aria-label={t("关闭上下文")} disabled={working} onClick={close}><X size={18} /></button></header>
      {snapshot ? <>
        <p className="context-total">{tx("约 {estimatedTokens}", { estimatedTokens: snapshot.estimatedTokens.toLocaleString() })} <span>/ {snapshot.window.toLocaleString()} tokens</span></p>
        <progress aria-label={t("上下文占用比例")} max={snapshot.window} value={Math.min(snapshot.estimatedTokens, snapshot.window)} />
        <dl><div><dt>{t("系统与指令（含记忆）")}</dt><dd>{snapshot.components.system.toLocaleString()}</dd></div><div><dt>{t("工具定义")}</dt><dd>{snapshot.components.tools.toLocaleString()}</dd></div><div><dt>{t("对话估算")}</dt><dd>{snapshot.components.conversation.toLocaleString()}</dd></div><div><dt>{t("输出预留")}</dt><dd>{snapshot.reserve.toLocaleString()}</dd></div></dl>
        <p className="fine-print">{tx("{value}实际用量以供应商回执为准。", { value: snapshot.source === "conservative_default" ? t("模型窗口未声明，当前采用保守值；可在模型的更多设置中填写。") : t("按当前模型配置估算。") })}</p>
        {snapshot.cost && <p className="fine-print context-cost">{tx("本会话费用：${usd}{value}{value2}", { usd: snapshot.cost.usd.toFixed(snapshot.cost.usd > 0 && snapshot.cost.usd < 0.01 ? 4 : 2), value: snapshot.cost.unpriced_requests ? t("（另有 {unpriced_requests} 次请求未计价）", { unpriced_requests: snapshot.cost.unpriced_requests }) : "", value2: snapshot.cost.limitUsd !== null ? t(" · 每个任务上限 ${limitUsd}", { limitUsd: snapshot.cost.limitUsd }) : "" })}</p>}
        {snapshot.last && <p className="fine-print">{tx("上次压缩：{before} → {after} tokens · {elapsedSeconds} 秒", { before: snapshot.last.before.toLocaleString(), after: snapshot.last.after.toLocaleString(), elapsedSeconds: snapshot.last.elapsedSeconds })}</p>}
        {Object.entries(snapshot.auxiliary).map(([name, value]) => <p className="fine-print" key={name}>{tx("{value}：{calls} 次 · 输入 {input_tokens} / 输出 {output_tokens} tokens", { value: ({ compaction: t("压缩"), native_compaction: t("原生压缩"), session_memory: t("会话摘要"), long_term_memory: t("长期记忆") } as Record<string, string>)[name] || name, calls: value.calls, input_tokens: value.input_tokens.toLocaleString(), output_tokens: value.output_tokens.toLocaleString() })}</p>)}
      </> : !error && <p>{t("正在读取…")}</p>}
      <label>{t("压缩时重点保留（可选）")}<input aria-label={t("压缩保留重点")} value={focus} maxLength={4000} placeholder={t("例如：当前目标、实验参数和未完成事项")} onChange={e => setFocus(e.target.value)} disabled={working} /></label>
      {error && <p role="alert" className="error">{tServer(error)}</p>}
      <footer><span className="fine-print">{t("压缩会调用已配置的模型；原文会归档。")}</span>{working ? <button onClick={() => void invoke("stop").catch(e => setError(String(e)))}><LoaderCircle size={14} className="spin" />{t("停止压缩")}</button> : <button className="primary" disabled={busy || !snapshot} onClick={() => void compact()}>{t("压缩上下文")}</button>}</footer>
    </section>
  </div>;
}
