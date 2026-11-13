import type { TurnTrace } from "./types";
import { t } from "./i18n.ts";

// A display projection of existing execution events, never a scheduler state.
export function turnPhase(trace?: TurnTrace): string {
  if (trace?.status !== "running") return "";
  if (trace.steps.some(step => step.kind === "compaction" && step.compaction.status === "running")) return t("正在压缩上下文");
  const streamStatus = trace.modelStreams?.at(-1)?.status;
  if (trace.runtimeStatus === "retrying" && streamStatus !== "slow_response") return t("模型正在重试");
  if (trace.steps.some(step => step.kind === "tool" && step.tool.status === "waiting")) return t("等待批准");
  if (trace.steps.some(step => step.kind === "tool" && ["running", "queued"].includes(step.tool.status))) return t("正在调用工具");
  const status = trace.runtimeStatus === "retrying" ? streamStatus : trace.runtimeStatus || streamStatus;
  return ({ planning_collaboration: t("正在制定协作决策"), waiting: t("等待接口响应"), connected: t("等待模型输出"), reasoning: t("模型正在思考"),
    slow_response: t("响应比预期更久，仍在等待…"), receiving: t("模型正在输出"), finishing: t("正在结束接收"), waiting_subagents: t("等待子代理返回"), tools: t("正在处理工具结果") } as Record<string, string>)[status || ""] || t("处理中");
}
