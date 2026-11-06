const test = require("node:test");
const assert = require("node:assert/strict");
const state = import("../src/state.ts");
const initial = {
  id: "one",
  project: "/project",
  config: { provider: "openai", model: "test", baseUrl: "", hasKey: false },
  capabilities: { skills: [], agents: [], settings: {} },
  sessions: [],
  history: [],
};
test("queued input is applied once and late acknowledgements cannot restore it", async () => {
  const { createSession, beginTurn, applyEvent } = await state;
  const input = {id: "additional", text: "change direction"};
  let session = beginTurn(createSession(initial), "original");
  session = applyEvent(session, {session: "one", type: "user_input_queued", input});
  session = applyEvent(session, {session: "one", type: "user_input_queued", input});
  assert.equal(session.pendingInputs.length, 1);
  const delivered = {session: "one", type: "user_input_applied", inputs: [input]};
  session = applyEvent(session, delivered);
  session = applyEvent(session, delivered);
  session = applyEvent(session, {session: "one", type: "user_input_queued", input});
  assert.equal(session.pendingInputs.length, 0);
  assert.deepEqual(session.messages.filter(m => m.role === "user").map(m => m.text), ["original", "change direction"]);
  assert.equal(session.streamIndex, session.messages.length - 1);
  assert.equal(session.busy, true);
  const other = {id: "withdrawn", text: "never deliver"};
  session = applyEvent(session, {session: "one", type: "user_input_queued", input: other});
  session = applyEvent(session, {session: "one", type: "user_input_withdrawn", id: other.id});
  assert.equal(session.pendingInputs.length, 0);
});
test("a failed request removes its empty assistant placeholder and Rich tags", async () => {
  const { createSession, beginTurn, applyEvent } = await state;
  const session = applyEvent(beginTurn(createSession(initial), "hello"), {
    session: "one", type: "turn_error", message: "[bold red]API Loop Exception:[/bold red] Connection error.",
  });
  assert.deepEqual(session.messages, [{ role: "user", text: "hello" }]);
  assert.equal(session.error, "API Loop Exception: Connection error.");
});
test("permission changes preserve the active draft and other session state", async () => {
  const { createSession, applyEvent } = await state;
  const session = { ...createSession(initial), draft: "keep", busy: true };
  assert.equal(session.permissionMode, "ask");
  const changed = applyEvent(session, { session: "one", type: "permission_mode_changed", permissionMode: "full" });
  assert.equal(changed.permissionMode, "full");
  assert.equal(changed.draft, "keep");
  assert.equal(changed.busy, true);
  assert.equal(applyEvent(changed, { session: "one", type: "permission_mode_changed", permissionMode: "invalid" }), changed);
});
test("events cannot alter another project session", async () => {
  const { createSession, applyEvent } = await state;
  const session = createSession(initial);
  assert.equal(
    applyEvent(session, {
      session: "two",
      type: "turn_error",
      message: "wrong session",
    }),
    session,
  );
});
test("streaming and final answer share one assistant message", async () => {
  const { createSession, beginTurn, applyEvent } = await state;
  let session = beginTurn(createSession(initial), "hello");
  session = applyEvent(session, {
    session: "one",
    type: "text_delta",
    text: "part",
  });
  session = applyEvent(session, {
    session: "one",
    type: "final",
    text: "complete",
  });
  session = applyEvent(session, { session: "one", type: "turn_finished" });
  assert.equal(session.messages.length, 2);
  assert.equal(session.messages[1].text, "complete");
  assert.equal(session.busy, false);
});

test("resumed output stays in the original response without a synthetic user prompt", async () => {
  const { createSession, beginTurn, applyEvent } = await state;
  let session = beginTurn(createSession(initial), "original");
  session = applyEvent(session, { session: "one", type: "turn_trace", trace: {
    id: "same", userMessageId: "human", status: "cancelled", steps: [], answer: "Partial " } });
  session = applyEvent(session, { session: "one", type: "turn_cancelled" });
  session = applyEvent(session, { session: "one", type: "turn_resumed", trace: {
    id: "same", userMessageId: "human", status: "running", steps: [], pendingText: "Partial " } });
  session = applyEvent(session, { session: "one", type: "text_delta", text: "continued" });
  assert.equal(session.messages[1].text, "Partial continued");
  session = applyEvent(session, { session: "one", type: "final", text: "Partial continued" });
  session = applyEvent(session, { session: "one", type: "turn_finished" });
  assert.equal(session.messages.length, 2);
  assert.equal(session.messages[0].id, "human");
  assert.equal(session.messages[1].trace.id, "same");
  assert.equal(session.busy, false);
});
test("parallel approvals resolve individually", async () => {
  const { createSession, applyEvent } = await state;
  let session = createSession(initial);
  session = applyEvent(session, {
    session: "one",
    type: "permission",
    requestId: "a",
  });
  session = applyEvent(session, {
    session: "one",
    type: "permission",
    requestId: "b",
  });
  session = applyEvent(session, {
    session: "one",
    type: "interaction_resolved",
    requestId: "a",
  });
  assert.deepEqual(
    session.interactions.map((item) => item.requestId),
    ["b"],
  );
});
test("worker completion preserves task identity and model", async () => {
  const { createSession, applyEvent } = await state;
  let session = createSession(initial);
  session = applyEvent(session, {
    session: "one",
    type: "subagent_status",
    agent: {
      agent_id: "a",
      status: "running",
      model: "review",
      task: "inspect",
    },
  });
  session = applyEvent(session, {
    session: "one",
    type: "subagent_status",
    agent: { agent_id: "a", status: "completed", result: "done" },
  });
  assert.equal(session.workers.length, 1);
  assert.equal(session.workers[0].model, "review");
  assert.equal(session.workers[0].result, "done");
});

test("engine loss clears live worker spinners without changing completed results", async () => {
  const { createSession, applyEvent } = await state;
  const previous = { ...createSession(initial), busy: true, workers: [
    { agent_id: "live", status: "running" }, { agent_id: "waiting", status: "queued" },
    { agent_id: "done", status: "completed", result: "kept" }], commands: { shell: { id: "shell", status: "running", owner: "main" } } };
  const result = applyEvent(previous, { session: "one", type: "engine_exit", message: "SSH result unconfirmed" });
  assert.deepEqual(result.workers.map(worker => worker.status), ["interrupted", "interrupted", "completed"]);
  assert.equal(result.workers[2].result, "kept");
  assert.equal(result.commands.shell.status, "interrupted");
  assert.equal(result.busy, false);assert.equal(previous.workers[0].status, "running");
  assert.equal(applyEvent(previous, { session: "one", type: "turn_error", message: "parent turn failed" }).workers, previous.workers);
});

test("activity handles out-of-order events and identical child tool IDs", async () => {
  const { createSession, applyEvent } = await state;
  let session = createSession(initial);
  session = applyEvent(session, { session: "one", type: "tool_started", tool_use_id: "same", tool_name: "bash_tool", input: { command: "echo main" }, timestamp: 1000 });
  session = applyEvent(session, { session: "one", type: "tool_started", agent_id: "child", tool_use_id: "same", tool_name: "bash_tool", input: { command: "echo child" }, timestamp: 1100 });
  session = applyEvent(session, { session: "one", type: "tool_result", tool_use_id: "same", result: "main result", timestamp: 2000 });
  session = applyEvent(session, { session: "one", type: "tool_scheduled", tool_use_id: "same", tool_name: "bash_tool" });
  assert.equal(session.tools.length, 2);
  assert.equal(session.tools[0].status, "completed");
  assert.equal(session.tools[0].name, "bash_tool");
  assert.equal(session.tools[0].input.command, "echo main");
  assert.equal(session.tools[1].status, "running");
  assert.equal(session.tools[1].agentId, "child");
  session = applyEvent(session, { session: "one", type: "turn_cancelled" });
  assert.equal(session.tools[1].status, "cancelled");
  assert.equal(session.tools[0].status, "completed");
});

test("workers keep batch order and original start time when results arrive out of order", async () => {
  const { createSession, applyEvent } = await state;
  let session = createSession(initial);
  for (const agent_id of ["a", "b", "c"]) session = applyEvent(session, { session: "one", type: "subagent_status", agent: { agent_id, status: "running", phase: "starting", started_at: 1000 } });
  session = applyEvent(session, { session: "one", type: "subagent_status", agent: { agent_id: "b", status: "running", phase: "running" } });
  session = applyEvent(session, { session: "one", type: "subagent_status", agent: { agent_id: "b", status: "completed", finished_at: 2000 } });
  assert.deepEqual(session.workers.map((worker) => worker.agent_id), ["a", "b", "c"]);
  assert.equal(session.tools.find((tool) => tool.id === "agent:b").startedAt, 1000);
  assert.equal(session.tools.find((tool) => tool.id === "agent:b").finishedAt, 2000);
});

test("only the resolved approval changes its activity state", async () => {
  const { createSession, applyEvent } = await state;
  let session = createSession(initial);
  for (const id of ["a", "b"]) {
    session = applyEvent(session, { session: "one", type: "tool_started", tool_use_id: id, tool_name: "file_write_tool" });
    session = applyEvent(session, { session: "one", type: "permission", requestId: id, request: { classifier_input: { tool_use_id: id, agent_role: "main" } } });
  }
  session = applyEvent(session, { session: "one", type: "interaction_resolved", requestId: "a" });
  assert.deepEqual(session.tools.map((tool) => tool.status), ["running", "waiting"]);
});

test("settled turn traces survive final replacement and remain isolated per question", async () => {
  const { createSession, beginTurn, applyEvent } = await state;
  let session = beginTurn(createSession(initial), "first");
  session = applyEvent(session, { session: "one", type: "text_delta", text: "working" });
  const trace = { id: "first", status: "completed", elapsedMs: 8000, steps: [{ kind: "text", text: "working" }], answer: "done" };
  session = applyEvent(session, { session: "one", type: "turn_trace", trace });
  session = applyEvent(session, { session: "one", type: "final", text: "done" });
  session = applyEvent(session, { session: "one", type: "turn_finished" });
  assert.equal(session.messages[1].trace.elapsedMs, 8000);
  session = beginTurn(session, "second");
  assert.deepEqual(session.messages[1].trace, trace);
  assert.deepEqual(session.messages[3].trace.steps, []);
});
