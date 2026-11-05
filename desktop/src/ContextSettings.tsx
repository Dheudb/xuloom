import type { ModelConfig } from "./types";
type Profile = NonNullable<ModelConfig["modelContext"]>[string];
export function ContextSettings({ provider, value, spec, change }: { provider: string; value?: Profile; spec?: NonNullable<ModelConfig['modelSpecs']>[string]; change: (value: Profile) => void }) {
  const profile: Profile = value || { window: 0, reserve: 0, keepRecent: 0, backend: "summary" };
  return <section className="model-option-section"><h4>上下文与压缩</h4>
    <div className="model-options-grid">
      <label>模型上下文窗口<input aria-label="模型上下文窗口" type="number" min="4096" max="16000000" placeholder={spec ? `官方：${spec.window.toLocaleString()} tokens` : "未知模型：暂按 65,536 tokens，可修改"} value={profile.window || ""} onChange={e => change({ ...profile, window: Number(e.target.value) })} /></label>
      <label>近期保留 tokens<input aria-label="近期保留 tokens" type="number" min="0" placeholder="自动按窗口分配" value={profile.keepRecent || ""} onChange={e => change({ ...profile, keepRecent: Number(e.target.value) })} /></label>
    </div>
    {spec && <p className="fine-print model-spec">官方规格：上下文 {spec.window.toLocaleString()} tokens · 最大输出 {spec.maxOutput.toLocaleString()} tokens。工具调用使用 Responses；单次输出额度与上下文窗口分别计算。</p>}
    <label>压缩方式<select aria-label="压缩方式" value={profile.backend} onChange={e => change({ ...profile, backend: e.target.value as Profile["backend"] })}>
      <option value="summary">通用文本摘要</option>{provider === "openai" && <option value="openai-responses">OpenAI Responses（需接口支持原生压缩）</option>}
    </select></label>
    <p className="fine-print">按此连接的当前模型保存。窗口包含输入和输出；原生方式要求接口支持 Responses 与 /responses/compact，未声明时不会调用。</p>
  </section>;
}
