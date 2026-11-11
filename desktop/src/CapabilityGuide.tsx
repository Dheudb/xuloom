import { useState } from "react";
import { X } from "lucide-react";
import type { SessionView } from "./types";
import { useDialogFocus } from './useDialogFocus';
import { t, tServer, tx } from "./i18n";

export default function CapabilityGuide({ kind, session, invoke, update, close }: {
  kind: "skills" | "agents"; session: SessionView;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  update: (patch: Partial<SessionView>) => void; close: () => void;
}) {
  const [error, setError] = useState("");
  const dialog=useDialogFocus();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const settings = session.capabilities.settings;
  const projectRoot = kind === "skills" ? settings.projectSkillsRoot : settings.projectAgentsRoot;
  const globalRoot = kind === "skills" ? settings.globalSkillsRoot : settings.globalAgentsRoot;
  const template = kind === "skills" ? t("---\nname: project-review\ndescription: Review code, structure and tests.\n---\n\n检查项目实现与测试，报告具体问题和文件路径。") :
    t("---\nname: reviewer\ndescription: Review code independently.\nmodel: inherit\ntools: Read, Grep, Glob, Skill\nskills: [project-review]\n---\n\n使用 project-review 技能独立审查代码，报告问题，不修改文件。");
  async function examples() {
    setBusy(true); setError("");
    try { update({ capabilities: await invoke("capabilities.example") }); setSaved(true); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  return <div className="overlay" onClick={close}>
    <section ref={dialog} className="dialog capability-guide" role="dialog" aria-modal="true" aria-label={t("能力配置说明")} onClick={(e) => e.stopPropagation()} onKeyDown={e=>{if(e.key==='Escape'){e.stopPropagation();close();}}}>
      <header><h2>{kind === "skills" ? t("配置 Skill") : t("配置 Subagent")}</h2><button className="icon-button" aria-label={t("关闭配置说明")} onClick={close}><X size={18} /></button></header>
      <p>{kind === "skills" ? t("每个技能使用一个目录，目录里的 SKILL.md 定义名称、使用时机和执行步骤。") : t("每个子代理使用一个 Markdown 文件，定义职责、工具、模型和需要加载的 Skills。")}</p>
      <p>{tx("当前项目：{value}{value2}所有项目共用：", { value: <code>{projectRoot}</code>, value2: <br /> })}<code>{globalRoot}</code></p>
      <div className="actions"><button onClick={() => invoke("capabilities.directory", { kind, scope: "project" }).catch((e) => setError(String(e)))}>{t("打开项目配置目录")}</button>
        <button onClick={() => invoke("capabilities.directory", { kind, scope: "global" }).catch((e) => setError(String(e)))}>{t("打开全局配置目录")}</button></div>
      <p>{tx("示例文件：{value}", { value: kind === "skills" ? "project-review/SKILL.md" : "reviewer.md" })}</p>
      <pre>{template}</pre>
      {kind === "skills" ? <p>{t("name 必须与技能目录名一致。导入技能目录会挂载原文件。在输入框输入 $ 可搜索并选择目录里的技能，打开列表时会重新扫描。选择后在输入行内显示蓝色技能名称，发送任务时加载完整指令；也可以点击“选择使用”。Xuloom 自带编程、研究、技能编写、数据分析、文档写作、资料总结、实验设计等九个技能。")}</p> :
        <p>{t("model: inherit 沿用当前模型；tools 限制可用工具；skills 填写有效的技能名称。点击角色卡片“分配任务”后补充任务并发送，模型通过子代理工具启动独立上下文。")}</p>}
      <p>{tx("{value}文件格式不正确时，面板会显示原因。", { value: kind === "skills" ? t("同名技能按项目目录、导入目录、环境目录、全局目录、内置目录的顺序选择。") : t("同名代理配置按项目目录、导入目录、全局目录的顺序选择。") })}</p>
      {error && <p role="alert" className="error">{tServer(error)}</p>}
      {saved && <p role="status">{t("已添加 project-review Skill 和 reviewer 子代理，可关闭说明后使用。")}</p>}
      <footer><button onClick={close}>{t("关闭")}</button><button className="primary" disabled={busy || session.busy || saved} onClick={examples}>{busy ? t("添加中…") : t("添加示例到当前项目")}</button></footer>
    </section>
  </div>;
}
