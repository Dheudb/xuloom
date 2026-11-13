const test = require("node:test");
const assert = require("node:assert/strict");
const notify = import("../src/turn-notify.ts");

test("a finished turn reports completion, failure or nothing for a user stop", async () => {
  const { finishedTurnText } = await notify;
  const outcomes = new Map();
  const finish = (session) => finishedTurnText(outcomes, { session, type: "turn_finished" });

  assert.equal(finishedTurnText(outcomes, { session: "a", type: "final", result: { ok: true } }), undefined);
  assert.equal(finish("a"), "任务已完成");

  finishedTurnText(outcomes, { session: "b", type: "turn_error" });
  assert.equal(finish("b"), "任务未完成，请回到会话查看结果");

  finishedTurnText(outcomes, { session: "c", type: "final", result: { ok: false } });
  assert.equal(finish("c"), "任务未完成，请回到会话查看结果");

  finishedTurnText(outcomes, { session: "d", type: "turn_cancelled" });
  finishedTurnText(outcomes, { session: "d", type: "turn_error" });
  assert.equal(finish("d"), undefined);
  assert.equal(outcomes.size, 0);
});

test("outcomes are tracked per session and reset when a new turn starts", async () => {
  const { finishedTurnText } = await notify;
  const outcomes = new Map();
  finishedTurnText(outcomes, { session: "a", type: "turn_error" });
  finishedTurnText(outcomes, { session: "b", type: "turn_cancelled" });
  assert.equal(finishedTurnText(outcomes, { session: "a", type: "managed_turn_started" }), undefined);
  assert.equal(finishedTurnText(outcomes, { session: "a", type: "turn_finished" }), "任务已完成");
  assert.equal(finishedTurnText(outcomes, { session: "b", type: "turn_finished" }), undefined);
});
