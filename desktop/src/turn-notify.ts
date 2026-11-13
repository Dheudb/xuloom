import { t } from "./i18n.ts";

export type TurnOutcome = "completed" | "incomplete" | "failed" | "cancelled";

type TurnEvent = { session: string; type: string; result?: { ok?: boolean } };

/** Remember how the turn ended; on turn_finished, return the notification text (nothing for a user stop). */
export function finishedTurnText(outcomes: Map<string, TurnOutcome>, event: TurnEvent): string | undefined {
  if (event.type === "final") outcomes.set(event.session, event.result?.ok === false ? "incomplete" : "completed");
  else if (event.type === "turn_error") outcomes.set(event.session, "failed");
  else if (event.type === "turn_cancelled") outcomes.set(event.session, "cancelled");
  else if (event.type === "managed_turn_started") outcomes.delete(event.session);
  if (event.type !== "turn_finished") return undefined;
  const outcome = outcomes.get(event.session) || "completed";
  outcomes.delete(event.session);
  if (outcome === "cancelled") return undefined;
  return outcome === "completed" ? t("任务已完成") : t("任务未完成，请回到会话查看结果");
}

/** Notify the system only while the workbench is in the background. */
export function notifyOnFinish(outcomes: Map<string, TurnOutcome>, event: TurnEvent) {
  const body = finishedTurnText(outcomes, event);
  if (body && !document.hasFocus())
    void window.xuloom.invoke("app.notify", { session: event.session, body }).catch(() => {});
}
