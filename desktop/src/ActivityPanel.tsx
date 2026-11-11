import { useEffect, useState } from "react";
import { Activity, ChevronDown } from "lucide-react";
import type { SessionView, ToolActivity } from "./types";
import { toolPresentation } from "./tool-presentation";
import ToolIcon from "./ToolIcon";
import ToolImages from "./ToolImages";
import { t, tx } from "./i18n";

export const statusText: Record<string, string> = { queued: t("准备中"), running: t("执行中"), waiting: t("等待批准"), completed: t("已完成"), error: t("失败"), crashed: t("异常"), cancelled: t("已取消"), interrupted: t("已中断") };
export const names: Record<string, string> = { media_tool: t("媒体生成"), agent_tool: t("管理子代理"), subagent: t("子代理任务"), bash_tool: t("执行命令"), command_tool: t("命令会话"), file_read_tool: t("读取文件"), file_write_tool: t("写入文件"), file_edit_tool: t("编辑文件"), grep_tool: t("搜索内容"), glob_tool: t("查找文件"), tool_search_tool: t("查找工具"), web_search_tool: t("搜索网页"), web_fetch_tool: t("读取网页"), skill_tool: t("加载技能") };
const pending = (tool: ToolActivity) => ["running", "queued", "waiting"].includes(tool.status);
const clip = (value: string) => value.length > 16000 ? t("{value}\n…内容较长，展示前 16000 个字符。", { value: value.slice(0, 16000) }) : value;
const resultPreview = (value: string) => value.includes("\nContent:\n") ? value.slice(value.indexOf("\nContent:\n") + 10) : value;

export default function ActivityPanel({ session, initialQuery = "" }: { session: SessionView; initialQuery?: string }) {
  const [query, setQuery] = useState(initialQuery);
  const [scope, setScope] = useState("all");
  useEffect(() => { setQuery(initialQuery); setScope("all"); }, [initialQuery]);
  const tools = session.tools.filter((tool) =>
    (scope === "all" || (scope === "main" ? !tool.agentId : !!tool.agentId)) &&
    JSON.stringify([tool.name, tool.input, tool.result, tool.agentRole, statusText[tool.status]]).toLowerCase().includes(query.toLowerCase()));
  return <>
    <div className="panel-title"><div><h3>{t("执行活动")} <span>{session.tools.length}</span></h3><p>{t("操作参数、执行状态与返回结果")}</p></div><Activity size={17} /></div>
    <div className="activity-toolbar">
      <div className="activity-counts"><span>{tx("{length} 进行中", { length: session.tools.filter(pending).length })}</span><span>{tx("{length} 已完成", { length: session.tools.filter((tool) => tool.status === "completed").length })}</span><span>{tx("{length} 异常", { length: session.tools.filter((tool) => ["error", "crashed"].includes(tool.status)).length })}</span></div>
      <div className="activity-filters"><input aria-label={t("搜索执行活动")} placeholder={t("搜索操作、文件或输出")} value={query} onChange={(event) => setQuery(event.target.value)} />
        <select aria-label={t("活动代理范围")} value={scope} onChange={(event) => setScope(event.target.value)}><option value="all">{t("全部代理")}</option><option value="main">{t("主代理")}</option><option value="children">{t("子代理")}</option></select></div>
    </div>
    <div className="panel-scroll activity-scroll">
      {!session.tools.length && <div className="instruction-empty"><Activity size={26} /><p>{t("还没有执行操作")}</p><small>{t("开始调用工具后，这里会显示操作详情；纯聊天不会产生工具活动。")}</small></div>}
      {!!session.tools.length && !tools.length && <p className="activity-empty">{t("没有符合筛选条件的活动。")}</p>}
      {[...tools].reverse().map((tool) => {
        const input = tool.input || {};
        const brief = input.description || input.command || input.file_path || input.path || input.query || input.prompt || input.action;
        const worker = session.workers.find((worker) => worker.agent_id === tool.agentId);
        const origin = tool.agentId ? worker?.description || tool.agentRole || t("子代理") : t("主代理");
        const elapsed = tool.startedAt && tool.finishedAt ? Math.max(0, tool.finishedAt - tool.startedAt) / 1000 : null;
        const view = toolPresentation(tool);
        const title = tool.name === "agent_tool" && input.action === "wait" ? t("等待子代理结果") : names[tool.name] || tool.name;
        return <details className={"tool-row activity-card " + tool.status} key={tool.id} data-activity-id={tool.id} data-status={tool.status}>
          <summary>
            <ToolIcon kind={view.kind} /><span className={"status-dot " + tool.status} />
            <span className="activity-heading"><strong>{title}</strong><span className="activity-origin" title={origin}>{origin}</span></span>
            <span className="activity-status">{statusText[tool.status] || tool.status}</span><ChevronDown size={13} />
            {brief && <span className="activity-brief">{String(brief)}</span>}
            <span className="activity-meta"><code>{tool.name}</code>{tool.startedAt && <time>{new Date(tool.startedAt).toLocaleTimeString("zh-CN", { hour12: false })}</time>}{elapsed !== null && <span>{tx("{elapsed} 秒", { elapsed: elapsed.toFixed(1) })}</span>}</span>
            {tool.result && <span className="activity-preview">{resultPreview(tool.result)}</span>}
          </summary>
          <div className="activity-detail">
            <ToolImages media={tool.media} sessionId={session.id} />
            <h4>{t("调用参数")}</h4><pre>{clip(JSON.stringify(input, null, 2))}</pre>
            <h4>{t("执行结果")}</h4><pre>{clip(tool.result || (tool.status === "waiting" ? t("等待你批准后执行。") : pending(tool) ? t("尚未返回结果。") : t("未返回输出。")))}</pre>
          </div>
        </details>;
      })}
      {session.logs.length > 0 && <details className="engine-logs"><summary>{tx("本次引擎日志 · {length}", { length: session.logs.length })}</summary><pre>{session.logs.join("\n")}</pre></details>}
    </div>
  </>;
}
