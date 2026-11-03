import type { ReasoningProfile } from "./types";

const labels: Record<string, string> = { default: "默认强度", none: "不推理", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" };
const adapters = [
  ["openai", "OpenAI"], ["deepseek", "DeepSeek"],
  ["qwen", "Qwen（云端）"], ["qwen-local", "Qwen（本地）"],
  ["anthropic", "Anthropic（自适应）"], ["anthropic-budget", "Anthropic（思考额度）"],
] as const;

export function ReasoningControls({ profile, disabled, change }: {
  profile?: ReasoningProfile; disabled: boolean;
  change: (value: Partial<Pick<ReasoningProfile, "thinking" | "effort">>) => void;
}) {
  if (!profile) return null;
  return <>
    {profile.adapter !== "openai" && <select className="thinking-picker" aria-label="Thinking 模式" title={profile.canDisable ? "开启或关闭思考；独立于协作档位" : "此模型不支持关闭思考"}
      disabled={disabled} value={profile.thinking === "disabled" ? "disabled" : "enabled"} onChange={event => change({ thinking: event.target.value as ReasoningProfile["thinking"] })}>
      <option value="enabled">开启思考</option>
      <option value="disabled" disabled={!profile.canDisable}>关闭思考</option>
    </select>}
    {!!profile.efforts.length && <select className="reasoning-picker" aria-label="推理强度" title="仅显示当前模型声明支持的档位；关闭 Thinking 时不发送强度参数"
      disabled={disabled || profile.thinking === "disabled"} value={profile.effort} onChange={event => change({ effort: event.target.value })}>
      {["default", ...profile.efforts].map(value => <option key={value} value={value}>{labels[value] || value}</option>)}
    </select>}
  </>;
}

export function ReasoningSettings({ provider, profile, change }: {
  provider: string; profile?: ReasoningProfile; change: (profile?: ReasoningProfile) => void;
}) {
  function choose(adapter: ReasoningProfile["adapter"]) {
    change({ adapter, efforts: adapter === "openai" || adapter === "anthropic" ? ["low", "medium", "high"] : adapter === "deepseek" ? ["low", "high", "max"] : [],
      canDisable: !["openai", "anthropic"].includes(adapter), thinking: adapter === "openai" ? "default" : "enabled", effort: "default", budgetTokens: 2048 });
  }
  return <fieldset className="reasoning-settings"><legend>思考设置</legend>
    <label>接口格式<select aria-label="推理参数格式" value={profile?.adapter || "unknown"} onChange={event => event.target.value === "unknown" ? change() : choose(event.target.value as ReasoningProfile["adapter"])}>
      <option value="unknown">跟随接口</option>
      {adapters.filter(([id]) => provider === "anthropic" ? id.startsWith("anthropic") : !id.startsWith("anthropic")).map(([id, title]) => <option key={id} value={id}>{title}</option>)}
    </select></label>
    {profile && <>
      {profile.adapter !== "openai" && <label className="reasoning-toggle"><input type="checkbox" aria-label="模型支持关闭 Thinking" checked={profile.canDisable}
        onChange={event => change({ ...profile, canDisable: event.target.checked, thinking: !event.target.checked && profile.thinking === "disabled" ? "enabled" : profile.thinking })} />支持关闭思考</label>}
      {profile.adapter !== "anthropic-budget" && <div role="group" aria-label="模型支持的推理强度" className="reasoning-efforts">
        <span>可用档位</span>
        <div className="reasoning-effort-options">{[...(profile.adapter === "openai" ? ["none"] : []), "minimal", "low", "medium", "high", "xhigh", "max"].map(value => <label key={value}><input type="checkbox" checked={profile.efforts.includes(value)}
          onChange={event => { const efforts = event.target.checked ? [...profile.efforts, value] : profile.efforts.filter(item => item !== value); change({ ...profile, efforts, effort: efforts.includes(profile.effort) ? profile.effort : "default" }); }} />{labels[value]}</label>)}
        </div>
      </div>}
      {profile.adapter === "anthropic-budget" && <label>Thinking token 预算<input type="number" aria-label="Thinking token 预算" min={1024} max={1000000} value={profile.budgetTokens}
        onChange={event => change({ ...profile, budgetTokens: Number(event.target.value) })} /></label>}
      <div className="reasoning-defaults"><ReasoningControls profile={profile} disabled={false} change={value => change({ ...profile, ...value })} /></div>
    </>}
    <details className="reasoning-help"><summary>如何选择？</summary><p className="fine-print">按模型接口文档选择；跟随接口时不附加参数。可选档位需由接口支持。</p></details>
  </fieldset>;
}
