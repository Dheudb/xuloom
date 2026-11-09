import type { ToolActivity } from "./types";

export type ToolKind = "sequence" | "image" | "edit" | "terminal" | "search" | "file" | "web" | "agent" | "skill" | "tool";
const basename = (value: unknown) => String(value || "").split(/[\\/]/).pop() || "";
const imagePath = /\.(png|jpe?g|webp|gif|bmp)(?:$|[?#])/i;
export function toolPresentation(tool: Pick<ToolActivity, "name" | "status" | "input" | "media">): { kind: ToolKind; title: string; brief: string } {
  const input = tool.input || {}, name = tool.name;
  const done = tool.status === "completed", failed = ["error", "crashed", "interrupted"].includes(tool.status);
  let kind: ToolKind = "tool", action = "调用工具", brief = String(input.description || input.action || name);
  if (name === 'ultra_prepare' || name === 'ultra_answer') {
    kind = 'agent'; action = name === 'ultra_prepare' ? '制定协作计划' : '判断简单任务'; brief = String(input.main_task || '');
  } else if (name === "tool_sequence") {
    kind = "sequence"; action = "执行工具序列"; brief = `${Array.isArray(input.steps) ? input.steps.length : 0} 个已确定步骤`;
  } else if (name === "media_tool") {
    kind = "image"; action = ({ submit: "提交媒体生成", status: "查询生成任务", cancel: "提交取消请求", capabilities: "查看生成连接", list: "查看生成记录" } as Record<string, string>)[String(input.action)] || "媒体生成";
    brief = String(input.model || input.job_id || "");
  } else if (name === "browser_tool" && ["render", "screenshot"].includes(String(input.action))) {
    kind = "image"; action = "截图检查"; brief = basename(input.file_path) || String(input.url || "网页");
  } else if (name === "view_image" || name === "file_read_tool" && imagePath.test(String(input.file_path || input.path || "")) || tool.media?.length) {
    kind = "image"; action = "查看图像";
    brief = tool.media?.length ? `${tool.media.length} 张` : basename(input.file_path || input.path);
  } else if (/^(file_(edit|write)_tool|notebook(_edit)?_tool|apply_patch)$/.test(name)) {
    kind = "edit"; action = name === "file_write_tool" ? "写入文件" : "编辑文件";
    brief = basename(input.file_path || input.notebook_path || input.path);
  } else if (/^(command_tool|bash_tool|repl_tool|task_(create|read|kill|list)_tool)$/.test(name)) {
    kind = "terminal"; action = name === "command_tool" && input.action === "poll" ? "读取命令输出" : "运行命令";
    brief = String(input.command || input.description || input.code || input.action || "");
  } else if (/search|grep|glob/.test(name)) {
    kind = "search"; action = name === "web_search_tool" ? "搜索网页" : name === "tool_search_tool" ? "查找工具" : "搜索文件";
    brief = String(input.query || input.pattern || input.description || "");
  } else if (name === "file_read_tool") {
    kind = "file"; action = "读取文件"; brief = basename(input.file_path || input.path);
  } else if (/fetch|browser/.test(name)) {
    kind = "web"; action = "读取网页"; brief = String(input.url || input.action || "");
  } else if (/agent|team|send_message/.test(name)) {
    kind = "agent"; action = name === "spawn_agent" || name === "agent_tool" && (!input.action || input.action === "spawn") ? "启动子代理" : input.action === "wait" ? "等待子代理" : "管理子代理";
    brief = String(input.description || input.action || "");
  } else if (name === "skill_tool") {
    kind = "skill"; action = "加载技能"; brief = String(input.name || input.skill || "");
  }
  return { kind, title: failed ? action + "失败" : tool.status === "cancelled" ? action + "已取消" : done ? "已" + action : action, brief };
}
