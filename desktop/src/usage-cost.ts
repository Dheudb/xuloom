import { t } from "./i18n.ts";

/** Native subtotals, with no inferred exchange rate. Missing maps are legacy USD. */
export function formatCosts(amounts: Record<string, number>) {
  const entries = Object.entries(amounts);
  return (entries.length ? entries : [["USD", 0] as [string, number]])
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, value]) => (currency === "CNY" ? "¥" : currency === "USD" ? "$" : currency + " ")
      + (value > 0 && value < 0.01 ? value.toFixed(4) : value.toFixed(2))).join(" + ");
}

/** "1,234 tokens · $0.0123" style label for one turn; undefined when the turn made no model request. */
export function usageText(usage?: { inputTokens: number; outputTokens: number; requests: number; unpricedRequests: number; estimated: boolean; costUsd?: number; costAmounts?: Record<string, number> }) {
  if (!usage || !usage.requests) return undefined;
  const tokens = `${usage.estimated ? t("约 ") : ""}${(usage.inputTokens + usage.outputTokens).toLocaleString()} tokens`;
  const amounts = usage.costAmounts ?? { USD: usage.costUsd ?? 0 };
  const money = formatCosts(amounts);
  const priced = usage.unpricedRequests === 0 ? money
    : Object.values(amounts).some(value => value > 0) ? t("{value} + 部分未计价", { value: money }) : t("未计价");
  const title = [t("输入 {inputTokens} / 输出 {outputTokens} tokens，{requests} 次模型请求", { inputTokens: usage.inputTokens.toLocaleString(), outputTokens: usage.outputTokens.toLocaleString(), requests: usage.requests }),
    usage.estimated ? t("服务未返回用量，tokens 为本地估算") : "",
    usage.unpricedRequests ? t("在 设置 → 费用 中填写模型价格即可计费；本地模型可填 0") : "",
    (usage.costAmounts !== undefined || usage.costUsd !== undefined) ? t("费用为整个任务累计，包含子代理") : ""].filter(Boolean).join("\n");
  return { text: `${tokens} · ${priced}`, title };
}
