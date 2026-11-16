import { useState } from "react";
import type { Capabilities } from "./types";
import { t, tServer, tx } from "./i18n";

export default function CapabilityGuide({ kind, settings, busy, invoke, changed }: {
  kind: "skills" | "agents"; settings: Capabilities["settings"]; busy: boolean;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  changed: (value: Capabilities) => void;
}) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [saved, setSaved] = useState(false);
  const projectRoot = kind === "skills" ? settings.projectSkillsRoot : settings.projectAgentsRoot;
  const globalRoot = kind === "skills" ? settings.globalSkillsRoot : settings.globalAgentsRoot;
  const template = kind === "skills" ? t("---\nname: project-review\ndescription: Review code, structure and tests.\n---\n\n检查项目实现与测试，报告具体问题和文件路径。") :
    t("---\nname: reviewer\ndescription: Review code independently.\nmodel: inherit\ntools: Read, Grep, Glob, Skill\nskills: [project-review]\n---\n\n使用 project-review 技能独立审查代码，报告问题，不修改文件。");
  async function examples() {
    setPending(true); setError("");
    try { changed(await invoke("capabilities.example")); setSaved(true); }
    catch (e) { setError(String(e)); } finally { setPending(false); }
  }
  return <details className="capability-guide">
    <summary>{kind === "skills" ? t("Skill 怎么配置？查看目录和示例") : t("Subagent 怎么配置？查看目录和示例")}</summary>
    <p>{kind === "skills" ? t("每个技能使用一个目录，目录里的 SKILL.md 定义名称、使用时机和执行步骤。") : t("每个子代理使用一个 Markdown 文件，定义职责、工具、模型和需要加载的 Skills。")}</p>
    <p>{tx("当前项目：{value}{value2}所有项目共用：", { value: <code>{projectRoot}</code>, value2: <br /> })}<code>{globalRoot}</code></p>
    <p>{tx("示例文件：{value}", { value: kind === "skills" ? "project-review/SKILL.md" : "reviewer.md" })}</p>
    <pre>{template}</pre>
    {kind === "skills" ? <p>{t("name 必须与技能目录名一致。导入技能目录会挂载原文件。在输入框输入 $ 可搜索并选择目录里的技能，打开列表时会重新扫描。选择后在输入行内显示蓝色技能名称，发送任务时加载完整指令。Xuloom 自带编程、研究、技能编写、数据分析、文档写作、资料总结、实验设计等九个技能。")}</p> :
      <p>{t("model: inherit 沿用当前模型；tools 限制可用工具；skills 填写有效的技能名称。点击角色的“分配任务”后补充任务并发送，模型通过子代理工具启动独立上下文。")}</p>}
    <p>{tx("{value}文件格式不正确时，列表会显示原因。", { value: kind === "skills" ? t("同名技能按项目 .xuloom、.agents、.claude、导入目录、环境目录、全局目录、内置目录的顺序选择。") : t("同名代理配置按项目目录、导入目录、全局目录的顺序选择。") })}</p>
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {saved && <p role="status">{t("已添加 project-review Skill 和 reviewer 子代理。")}</p>}
    <div className="actions"><button className="primary" disabled={pending || busy || saved} onClick={examples}>{pending ? t("添加中…") : t("添加示例到当前项目")}</button></div>
  </details>;
}
