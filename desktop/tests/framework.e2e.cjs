const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-framework-e2e-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [data, config]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null });
const python = process.env.XULOOM_TEST_EXECUTABLE
  ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe")
  : path.join(root, "../.venv-mcp/Scripts/python.exe");
const failures = [], children = [];
const payload = '中文 input "quotes" $HOME `literal`\nsecond line';
let app, page, workerId, commandId, sequence = 0;
function reply(res, text, name, input) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
  if (name) {
    chunk({ role: "assistant", tool_calls: [{ index: 0, id: "call_" + ++sequence, type: "function", function: { name, arguments: JSON.stringify(input) } }] });
    chunk({}, "tool_calls");
  } else { chunk({ role: "assistant", content: text }); chunk({}, "stop"); }
  res.end("data: [DONE]\n\n");
}
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
      // The main agent starts with a lean tool set (agent tools are deferred), so identify the child by its task, not by missing schemas.
      const isChild = JSON.stringify(body.messages.find((m) => m.role === "user")?.content || "").includes("FIRST_MEMORY");
      if (isChild) {
        assert.equal(body.reasoning_effort, "high");
        assert.equal(body.model, "worker-fixture");
        const text = JSON.stringify(body.messages);
        assert(text.includes("FIRST_MEMORY"));
        if (children.length > 0) assert(text.includes("FIRST_REPLY"), "Continuation must preserve the earlier assistant answer");
        if (children.length === 1) assert(text.includes("QUEUED_NOTE"), "Queued message must reach the actual resumed model request");
        if (children.length === 2) assert(text.includes("SECOND_REPLY"), "Restart must preserve the second child turn");
        children.push(body);
        return reply(res, ["FIRST_REPLY", "SECOND_REPLY", "THIRD_REPLY"][children.length - 1]);
      }
      const user = [...body.messages].reverse().find((m) => m.role === "user");
      const after = body.messages.slice(body.messages.indexOf(user) + 1);
      const tools = after.filter((m) => m.role === "tool");
      const result = tools.length ? JSON.parse(tools.at(-1).content) : null;
      if (user.content === "测试通用框架") {
        switch (tools.length) {
          case 0: return reply(res, null, "agent_tool", { prompt: "FIRST_MEMORY", model: "worker-fixture", reasoning_effort: "high", response_format: "json", run_in_background: false });
          case 1:
            assert.equal(result.agents[0].status, "completed"); workerId = result.agents[0].agent_id;
            return reply(res, null, "agent_tool", { action: "send", agent_id: workerId, prompt: "QUEUED_NOTE" });
          case 2:
            assert.equal(result.requires_followup, true);
            return reply(res, null, "agent_tool", { action: "followup", agent_id: workerId, prompt: "SECOND", response_format: "json", run_in_background: false });
          case 3:
            assert.equal(result.agent_id, workerId); assert.equal(result.turn, 2); assert.equal(result.pending_message_count, 0);
            return reply(res, null, "agent_tool", { action: "capacity" });
          case 4:
            assert.equal(result.active, 0); assert.equal(result.limit, null); assert.equal(result.available, null);
            return reply(res, null, "command_tool", { argv: [python, "-c", "import sys,json; print(json.dumps({'echo':sys.stdin.read()},ensure_ascii=False))"], stdin: payload, wait_ms: 15000 });
          case 5:
            assert.equal(result.status, "completed"); assert.equal(JSON.parse(result.output.text).echo, payload);
            assert(result.cwd.toLowerCase().startsWith(data.toLowerCase())); commandId = result.id;
            return reply(res, "FRAMEWORK_PASS");
          default: throw new Error("Unexpected control call");
        }
      }
      if (user.content === "重启后继续") {
        if (tools.length === 0) return reply(res, null, "agent_tool", { action: "followup", agent_id: workerId, prompt: "THIRD", response_format: "json", run_in_background: false });
        if (tools.length === 1) {
          assert.equal(result.agent_id, workerId); assert.equal(result.turn, 3);
          return reply(res, null, "command_tool", { action: "read", command_id: commandId });
        }
        assert.equal(JSON.parse(result.output.text).echo, payload);
        return reply(res, "RESTART_PASS");
      }
      if (user.content === "后台命令测试") {
        if (!tools.length) return reply(res, null, "command_tool", { argv: [python, "-c", "import time; print('READY',flush=True); time.sleep(30)"], wait_ms: 10 });
        assert.equal(result.status, "running");
        return reply(res, "BACKGROUND_STARTED");
      }
      throw new Error("Unexpected request: " + user?.content);
    } catch (error) { failures.push(error.stack); res.writeHead(500).end("fixture assertion failed"); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000);
  page.on("pageerror", (error) => failures.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text, expected) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.getByText(expected, { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
(async () => {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "main-fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch();
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("测试通用框架", "FRAMEWORK_PASS");
    await app.close(); app = null;
    await launch(); await page.locator(".session-item.active").waitFor();
    await send("重启后继续", "RESTART_PASS");
    await send("后台命令测试", "BACKGROUND_STARTED");
    await page.getByRole("button", { name: "1 个后台命令正在运行 · 停止所有后台任务", exact: true }).click();
    await page.getByRole("button", { name: "1 个后台命令正在运行 · 停止所有后台任务", exact: true }).waitFor({ state: "detached" });
    assert.equal(children.length, 3);
    assert.deepEqual(failures, []);
    console.log("Framework E2E passed: same worker across turns/restart, queued message delivery, model/effort inheritance, capacity, exact UTF-8 stdin and persisted command output");
  } finally {
    if (app) await app.close().catch(() => {});
    server.closeAllConnections(); server.close();
  }
})().catch((error) => { console.error(error); console.error(failures); process.exitCode = 1; });
