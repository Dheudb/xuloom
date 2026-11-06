import type { TurnTrace } from "./types";

// A display projection of existing execution events, never a scheduler state.
export function turnPhase(trace?: TurnTrace): string {
  if (trace?.status !== "running") return "";
  if (trace.steps.some(step => step.kind === "compaction" && step.compaction.status === "running")) return "正在压缩上下文";
  if (trace.runtimeStatus === "retrying") return "模型正在重试";
  if (trace.steps.some(step => step.kind === "tool" && step.tool.status === "waiting")) return "等待批准";
  if (trace.steps.some(step => step.kind === "tool" && ["running", "queued"].includes(step.tool.status))) return "正在调用工具";
  const status = trace.runtimeStatus || trace.modelStreams?.at(-1)?.status;
  return ({ planning_collaboration: "正在制定协作决策", waiting: "等待接口响应", connected: "等待模型输出", reasoning: "模型正在思考",
    receiving: "模型正在输出", finishing: "正在结束接收", waiting_subagents: "等待子代理返回", tools: "正在处理工具结果" } as Record<string, string>)[status || ""] || "处理中";
}
