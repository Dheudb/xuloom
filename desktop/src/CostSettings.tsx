import { useEffect, useState } from "react";
import { Plus, Trash2, Check } from "lucide-react";
import { t, tServer, tx } from "./i18n";

type Price = { input: number; output: number; cache_write?: number; cache_read?: number; currency?: "USD" | "CNY" };
type Pricing = { models: Record<string, Price>; maxCostUsd: number | null; maxCostCny?: number | null; cnyCapFromEnvironment?: boolean; path: string; capFromEnvironment: boolean };
type Row = { currency: "USD" | "CNY"; connection: string; model: string; input: string; output: string; cacheRead: string; cacheWrite: string };

const toRows = (models: Record<string, Price>): Row[] => Object.entries(models).map(([model, price]) => ({
  currency: price.currency || "USD",
  connection: model.startsWith("@") ? model.slice(1, model.indexOf("/")) : "",
  model: model.startsWith("@") ? model.slice(model.indexOf("/") + 1) : model, input: String(price.input), output: String(price.output),
  cacheRead: price.cache_read === undefined ? "" : String(price.cache_read),
  cacheWrite: price.cache_write === undefined ? "" : String(price.cache_write),
}));

/** Model prices (native currency per million tokens) and the per-task cost cap, kept in ~/.xuloom/pricing.json. */
export default function CostSettings({ invoke }: { invoke: (method: string, params?: any) => Promise<any> }) {
  const [pricing, setPricing] = useState<Pricing>();
  const [connections, setConnections] = useState<{connectionId: string; connectionName: string}[]>([]);
  useEffect(() => { let live = true; invoke("config.get").then(value => { if (live) setConnections(value.connections || []); }).catch(() => {}); return () => { live = false; }; }, []);
  const [rows, setRows] = useState<Row[]>([]);
  const [cap, setCap] = useState("");
  const [cnyCap, setCnyCap] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let live = true;
    invoke("pricing.get").then((value: Pricing) => {
      if (!live) return;
      setPricing(value); setRows(toRows(value.models)); setCap(value.maxCostUsd === null ? "" : String(value.maxCostUsd));
      setCnyCap(value.maxCostCny == null ? "" : String(value.maxCostCny));
    }).catch(e => { if (live) setError(String(e)); });
    return () => { live = false; };
  }, []);
  const update = (index: number, patch: Partial<Row>) => { setSaved(false); setRows(rows.map((row, i) => i === index ? { ...row, ...patch } : row)); };

  async function save() {
    setError(""); setSaved(false);
    const models: Record<string, Price> = {};
    for (const row of rows) {
      const model = row.model.trim();
      const name = row.connection && model ? "@" + row.connection + "/" + model : model;
      if (!name && !row.input && !row.output) continue;
      if (!name) return setError(t("请填写模型名称或通配符，例如 gpt-4.1 或 llama*。"));
      if (models[name]) return setError(t("模型 {name} 重复了。", { name }));
      const numbers = [row.input, row.output].map(Number);
      if (row.input === "" || row.output === "" || numbers.some(value => !Number.isFinite(value) || value < 0))
        return setError(t("模型 {name} 需要填写不小于 0 的输入和输出价格；本地或免费模型填 0。", { name }));
      models[name] = { input: numbers[0], output: numbers[1], currency: row.currency };
      if (row.cacheRead !== "") models[name].cache_read = Number(row.cacheRead);
      if (row.cacheWrite !== "") models[name].cache_write = Number(row.cacheWrite);
    }
    const limit = cap.trim() === "" ? null : Number(cap);
    if (limit !== null && (!Number.isFinite(limit) || limit < 0)) return setError(t("花费上限需要是不小于 0 的数字，留空表示不限制。"));
    const cnyLimit = cnyCap.trim() === "" ? null : Number(cnyCap);
    if (cnyLimit !== null && (!Number.isFinite(cnyLimit) || cnyLimit < 0)) return setError(t("花费上限需要是不小于 0 的数字，留空表示不限制。"));
    setSaving(true);
    try {
      const value: Pricing = await invoke("pricing.save", { models, maxCostUsd: limit, maxCostCny: cnyLimit });
      setPricing(value); setRows(toRows(value.models)); setSaved(true);
    } catch (e) { setError(String(e)); } finally { setSaving(false); }
  }

  return <section className="cost-settings">
    <h3>{t("费用")}</h3>
    <p className="fine-print">{t("按每百万 tokens 的原币价格填写，可选择美元或人民币。费用分别累计，不自动换汇；未填写价格的模型标为“未计价”。")}</p>
    <p className="fine-print">{t("本地模型或免费额度填 0，按所选币种计为零。模型名支持通配符，例如")} <code>llama*</code>、<code>claude-sonnet-*</code>。</p>
    {!pricing && !error && <p role="status" className="fine-print">{t("正在读取…")}</p>}
    {pricing && <>
      <table className="cost-table" aria-label={t("模型价格")}>
        <thead><tr><th>{t("API 连接")}</th><th>{t("模型或通配符")}</th><th>{t("币种")}</th><th>{t("输入")}</th><th>{t("输出")}</th><th>{t("缓存读取")}</th><th>{t("缓存写入")}</th><th /></tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>
          <td><select aria-label={t("价格适用连接")} value={row.connection} onChange={e => update(index, { connection: e.target.value })}>
            <option value="">{t("所有连接（默认价格）")}</option>
            {connections.map(c => <option key={c.connectionId} value={c.connectionId}>{c.connectionName}</option>)}
            {row.connection && !connections.some(c => c.connectionId === row.connection) && <option value={row.connection}>{row.connection}</option>}
          </select></td>
          <td><input aria-label={t("模型名称")} value={row.model} placeholder="gpt-4.1" onChange={e => update(index, { model: e.target.value })} /></td>
          <td><select className="cost-currency" aria-label={t("价格币种")} value={row.currency} onChange={e => update(index, { currency: e.target.value as "USD" | "CNY" })}><option value="USD">USD $</option><option value="CNY">CNY ¥</option></select></td>
          <td><input aria-label={t("输入价格")} inputMode="decimal" value={row.input} placeholder="2" onChange={e => update(index, { input: e.target.value })} /></td>
          <td><input aria-label={t("输出价格")} inputMode="decimal" value={row.output} placeholder="8" onChange={e => update(index, { output: e.target.value })} /></td>
          <td><input aria-label={t("缓存读取价格")} inputMode="decimal" value={row.cacheRead} placeholder={t("同输入")} onChange={e => update(index, { cacheRead: e.target.value })} /></td>
          <td><input aria-label={t("缓存写入价格")} inputMode="decimal" value={row.cacheWrite} placeholder={t("同输入")} onChange={e => update(index, { cacheWrite: e.target.value })} /></td>
          <td><button aria-label={t("删除 {value}", { value: row.model || t("这一行") })} onClick={() => { setSaved(false); setRows(rows.filter((_, i) => i !== index)); }}><Trash2 size={15} /></button></td>
        </tr>)}</tbody>
      </table>
      <button className="cost-add" onClick={() => { setSaved(false); setRows([...rows, { currency: "USD", connection: "", model: "", input: "", output: "", cacheRead: "", cacheWrite: "" }]); }}><Plus size={15} />{t("添加模型价格")}</button>
      <label className="cost-cap">{t("每个任务的花费上限（美元）")}<input aria-label={t("任务花费上限")} inputMode="decimal" value={cap} placeholder={t("不限制")} onChange={e => { setSaved(false); setCap(e.target.value); }} />
      </label>
      <label className="cost-cap">{t("每个任务的花费上限（人民币）")}<input aria-label={t("人民币任务花费上限")} inputMode="decimal" value={cnyCap} placeholder={t("不限制")} onChange={e => { setSaved(false); setCnyCap(e.target.value); }} /></label>
      <p className="fine-print">{t("设置预算后，每个使用的币种都需要对应上限；美元和人民币分别检查，不自动换算。")}</p>
      <p className="fine-print">{t("这是停止条件，不是账单保证：达到上限后，任务不再发出新的模型请求，子代理计入同一个任务；已经发出的请求（包括并行子代理的请求）仍会完成并计费，所以最终花费可能超过上限。费用按这里填写的价格计算，以供应商账单为准。设置了上限时，没有价格的模型会被拒绝运行；本地模型请把价格填 0。")}</p>
      {pricing.capFromEnvironment && <p className="fine-print">{t("提示：环境变量 XULOOM_MAX_COST_USD 已设置，它优先于这里的上限。")}</p>}
      {pricing.cnyCapFromEnvironment && <p className="fine-print">{t("提示：环境变量 XULOOM_MAX_COST_CNY 已设置，它优先于这里的人民币上限。")}</p>}
      <footer className="cost-footer"><span className="fine-print">{tx("保存到 {path}，与 CLI 共用；已打开的本机会话从下一次请求开始使用。", { path: pricing.path })}</span>
        <button className="primary" disabled={saving} onClick={() => void save()}>{saved ? <><Check size={15} />{t("已保存")}</> : t("保存")}</button></footer>
    </>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
  </section>;
}

export { usageText } from "./usage-cost";
