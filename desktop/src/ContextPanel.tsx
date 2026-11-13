import { useEffect, useState } from "react";
import { LoaderCircle, ChevronRight } from "lucide-react";
import type { ModelConfig } from "./types";
import { getLanguage, t, tServer, tx } from "./i18n";
type Snapshot = { estimatedTokens: number; window: number; reserve: number; source: string; estimator: string;
  components: { system: number; tools: number; conversation: number; memoryCharacters: number };
  breakdown?: { messages: number; tools: number; mcp: number; system: number; memory: number;
    deferredTools: number; deferredMcp: number; mcpTools: { name: string; active: boolean }[];
    memoryFiles: { path: string; truncated: boolean }[] };
  last?: { before: number; after: number; elapsedSeconds: number; backend: string };
  auxiliary: Record<string, { calls: number; input_tokens: number; output_tokens: number; cost_usd?: number }>;
  cost?: { usd: number; unpriced_requests: number; limitUsd: number | null } };
function ContextUsage({ snapshot }: { snapshot: Snapshot }) {
  const { estimatedTokens, window, reserve, breakdown, components } = snapshot;
  const locale = getLanguage() === "en" ? "en" : "zh-CN";
  const number = new Intl.NumberFormat(locale);
  const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
  const percent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 });
  const free = Math.max(0, window - estimatedTokens - reserve);
  const rows = [
    { key: "messages", label: t("消息"), tokens: breakdown?.messages ?? components.conversation },
    { key: "tools", label: breakdown ? t("系统工具") : t("工具定义"), tokens: breakdown?.tools ?? components.tools },
    { key: "mcp", label: t("MCP 工具"), tokens: breakdown?.mcp ?? 0 },
    { key: "system", label: breakdown ? t("系统提示") : t("系统与指令（含记忆）"), tokens: breakdown?.system ?? components.system },
    { key: "memory", label: t("记忆文件"), tokens: breakdown?.memory ?? 0 },
  ].filter(row => row.tokens > 0);
  const capacity = [
    { key: "reserve", label: t("输出预留"), tokens: reserve },
    { key: "free", label: t("剩余空间"), tokens: free },
  ];
  const used = percent.format(estimatedTokens / window);
  let remaining = window;
  const segments = [...rows, ...capacity].map(row => {
    const width = Math.min(row.tokens, remaining);
    remaining -= width;
    return { ...row, width };
  });
  function usageRow(row: typeof rows[number]) {
    return <div key={row.key} className="context-usage-row" data-context-kind={row.key}>
      <dt><i className="context-swatch" aria-hidden="true" />{row.label}</dt>
      <dd title={number.format(row.tokens) + " tokens"}>{compact.format(row.tokens)}</dd>
      <dd>{percent.format(row.tokens / window)}</dd>
    </div>;
  }
  return <>
    <div className="context-total">
      <strong>{t("约已使用 {percent}", { percent: used })}</strong>
      <span title={number.format(estimatedTokens) + " / " + number.format(window) + " tokens"}>{compact.format(estimatedTokens)} / {compact.format(window)} tokens</span>
    </div>
    <div className="context-meter" role="meter" aria-label={t("上下文占用比例")} aria-valuemin={0} aria-valuemax={window}
      aria-valuenow={Math.min(estimatedTokens, window)} aria-valuetext={t("约已使用 {percent}", { percent: used })}>
      {segments.filter(row => row.width > 0).map(row => <span key={row.key} data-context-kind={row.key}
        style={{ width: `${row.width / window * 100}%` }} title={`${row.label} · ${number.format(row.tokens)} tokens`} />)}
    </div>
    <dl className="context-usage">{rows.map(usageRow)}</dl>
    <dl className="context-usage context-capacity">{capacity.map(usageRow)}</dl>
    {breakdown && (breakdown.deferredTools > 0 || breakdown.deferredMcp > 0) && <dl className="context-usage context-deferred">
      {breakdown.deferredMcp > 0 && <div className="context-usage-row"><dt>{t("待加载 MCP 工具")}</dt><dd>{number.format(breakdown.deferredMcp)}</dd><dd aria-hidden="true">—</dd></div>}
      {breakdown.deferredTools > 0 && <div className="context-usage-row"><dt>{t("待加载系统工具")}</dt><dd>{number.format(breakdown.deferredTools)}</dd><dd aria-hidden="true">—</dd></div>}
    </dl>}
    {!!breakdown?.mcpTools.length && <details className="context-detail">
      <summary><ChevronRight size={14} aria-hidden="true" />{t("MCP 工具")}<span>{number.format(breakdown.mcpTools.length)}</span></summary>
      <ul>{breakdown.mcpTools.map(tool => <li key={tool.name}><code>{tool.name}</code><span>{tool.active ? t("已加载") : t("待加载")}</span></li>)}</ul>
    </details>}
    {!!breakdown?.memoryFiles.length && <details className="context-detail">
      <summary><ChevronRight size={14} aria-hidden="true" />{t("记忆文件")}<span>{number.format(breakdown.memoryFiles.length)}</span></summary>
      <ul>{breakdown.memoryFiles.map(file => <li key={file.path}><code title={file.path}>{file.path.split(/[\\/]/).pop()}</code>{file.truncated && <span>{t("部分载入")}</span>}</li>)}</ul>
    </details>}
  </>;
}

export default function ContextPanel({ invoke, busy, working, model, compactError, setCompaction }: {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  busy: boolean;
  working: boolean;
  model?: ModelConfig;
  compactError?: string;
  setCompaction: (working: boolean, error?: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [focus, setFocus] = useState("");
  const [error, setError] = useState("");
  const window = model?.modelContext?.[model.model]?.window;
  const reserve = model?.modelContext?.[model.model]?.reserve;
  useEffect(() => {
    if (working) return;
    let live = true;
    invoke("context.get").then(value => { if (live) { setSnapshot(value); setError(""); } })
      .catch(e => { if (live) setError(String(e)); });
    return () => { live = false; };
  }, [invoke, busy, working, model?.connectionId, model?.model, window, reserve]);

  async function compact() {
    setCompaction(true);
    setError("");
    let failure: string | undefined;
    try { setSnapshot(await invoke("context.compact", { focus })); }
    catch (e) { failure = String(e); }
    finally { setCompaction(false, failure); }
  }
  return <section className="panel-scroll context-panel" aria-label={t("上下文用量")}>
      {snapshot ? <>
        <ContextUsage snapshot={snapshot} />
        <p className="fine-print">{tx("{value}实际用量以供应商回执为准。", { value: snapshot.source === "conservative_default" ? t("模型窗口未声明，当前采用保守值；可在模型的更多设置中填写。") : t("按当前模型配置估算。") })}</p>
        {snapshot.cost && <p className="fine-print context-cost">{tx("本会话费用：${usd}{value}{value2}", { usd: snapshot.cost.usd.toFixed(snapshot.cost.usd > 0 && snapshot.cost.usd < 0.01 ? 4 : 2), value: snapshot.cost.unpriced_requests ? t("（另有 {unpriced_requests} 次请求未计价）", { unpriced_requests: snapshot.cost.unpriced_requests }) : "", value2: snapshot.cost.limitUsd !== null ? t(" · 每个任务上限 ${limitUsd}", { limitUsd: snapshot.cost.limitUsd }) : "" })}</p>}
        {snapshot.last && <p className="fine-print">{tx("上次压缩：{before} → {after} tokens · {elapsedSeconds} 秒", { before: snapshot.last.before.toLocaleString(), after: snapshot.last.after.toLocaleString(), elapsedSeconds: snapshot.last.elapsedSeconds })}</p>}
        {Object.entries(snapshot.auxiliary).map(([name, value]) => <p className="fine-print" key={name}>{tx("{value}：{calls} 次 · 输入 {input_tokens} / 输出 {output_tokens} tokens", { value: ({ compaction: t("压缩"), native_compaction: t("原生压缩"), session_memory: t("会话摘要"), long_term_memory: t("长期记忆") } as Record<string, string>)[name] || name, calls: value.calls, input_tokens: value.input_tokens.toLocaleString(), output_tokens: value.output_tokens.toLocaleString() })}</p>)}
      </> : !error && <p>{t("正在读取…")}</p>}
      <label>{t("压缩时重点保留（可选）")}<input aria-label={t("压缩保留重点")} value={focus} maxLength={4000} placeholder={t("例如：当前目标、实验参数和未完成事项")} onChange={e => setFocus(e.target.value)} disabled={working} /></label>
      {(error || compactError) && <p role="alert" className="error">{tServer(error || compactError)}</p>}
      <footer><span className="fine-print">{t("压缩会调用已配置的模型；原文会归档。")}</span>{working ? <button onClick={() => void invoke("stop").catch(e => setError(String(e)))}><LoaderCircle size={14} className="spin" />{t("停止压缩")}</button> : <button className="primary" disabled={busy || !snapshot} onClick={() => void compact()}>{t("压缩上下文")}</button>}</footer>
  </section>;
}
