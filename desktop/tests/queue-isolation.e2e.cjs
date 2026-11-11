const { _electron: electron } = require("playwright");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), http = require("node:http"), assert = require("node:assert/strict");
const { SessionCatalog, atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-queue-isolation-"));
const project = path.join(fixture, "workspace"), data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [project, data, config]) fs.mkdirSync(dir);
let settings = { projects: [project] };
const catalog = new SessionCatalog(() => settings, value => settings = value, data), row = catalog.create(project);
settings.activeConversation = { project, resumeId: row.sessionId, scope: "project" };
atomicJSON(path.join(data, "workbench.json"), settings);
let app, page, release = false, started = 0, active = 0, peak = 0, ended = 0;
const pending = [], errors = [];
function reply(res, text, call) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const delta = call ? { tool_calls: [{ index: 0, id: "dispatch", type: "function", function: { name: call.name, arguments: JSON.stringify(call.input) } }] } : { content: text };
  res.end("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason: null }] }) + "\n\ndata: " + JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: call ? "tool_calls" : "stop" }] }) + "\n\ndata: " + JSON.stringify({ choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }) + "\n\ndata: [DONE]\n\n");
}
function finish(res) { setTimeout(() => { active--; ended++; reply(res, "WORKER_" + ended); }, 150); }
const server = http.createServer((req, res) => { let raw = ""; req.on("data", chunk => raw += chunk); req.on("end", () => {
  try {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
    const child = body.messages.some(m => m.role === "system" && String(m.content).includes("You are a delegated subagent"));
    if (child) { started++; active++; peak = Math.max(peak, active); if (release) finish(res); else pending.push(res); return; }
    const human = [...body.messages].reverse().find(m => m.role === "user" && typeof m.content === "string" && (m.content.includes("QUEUE_TEST") || m.content.includes("ISOLATION_TEST")));
    const after = human ? body.messages.slice(body.messages.indexOf(human) + 1) : [];
    if (String(human?.content).includes("QUEUE_TEST")) {
      if (!after.some(m => m.role === "tool")) reply(res, "", { name: "agent_tool", input: { prompt: "你好", description: "Queue worker", count: 10, run_in_background: true } });
      else reply(res, ended === 10 ? "ALL_TEN_DONE" : "PROVISIONAL_DRAFT");
    } else {
      assert(!body.tools.some(t => ["web_fetch_tool", "bash_tool"].includes(t.function?.name)), "Unconfined tools must be absent from isolation schemas");
      if (!after.some(m => m.role === "tool")) reply(res, "", { name: "file_write_tool", input: { file_path: path.join(project, "isolated-result.md"), content: "隔离文件工具成功\n" } });
      else reply(res, "ISOLATION_DONE");
    }
  } catch (error) { errors.push(error.message); res.writeHead(500).end("fixture failure"); }
}); });
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER", "XULOOM_EXECUTION_MODE"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(90000); page.on("pageerror", e => errors.push(e.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
}
(async () => { try {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
  atomicJSON(path.join(project,".xuloom/capabilities.json"),{agent_limits:{concurrency:3}});
  await launch();
  await page.getByLabel("访问权限", { exact: true }).selectOption("full");
  await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
  await page.evaluate(() => { window.queueEvents = []; window.xuloom.onEvent(event => { if (event.type === "subagent_status") window.queueEvents.push(event.agent); }); });
  await send("QUEUE_TEST 启动十个子代理，问你好并汇总所有结果");
  await page.waitForFunction(() => window.queueEvents.filter(w => w.status === "running").length >= 3);
  // Runtime running means the task started; HTTP may arrive later on a busy host.
  const requestDeadline = Date.now() + 90000;
  while (started < 3 && Date.now() < requestDeadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(started, 3); assert.equal(ended, 0);
  const states = await page.evaluate(() => { const map = new Map(); for (const w of window.queueEvents) map.set(w.agent_id, w.status); return [...map.values()]; });
  assert.equal(states.filter(s => s === "queued").length, 7);
  assert.equal(await page.locator(".send-button.stop").count(), 1, "Parent must wait for queued workers");
  release = true; pending.splice(0).forEach(finish);
  await page.waitForFunction(() => window.queueEvents.filter(w => w.status === "completed").length === 10);
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
  assert.equal(peak, 3); assert.equal(started, 10); assert.equal(ended, 10);
  await page.getByText("ALL_TEN_DONE", { exact: true }).waitFor();
  await page.getByRole("button", { name: "添加工具与模式", exact: true }).click();
  await page.getByRole("menuitem", { name: /执行隔离/ }).click();
  const dialog = page.getByRole("dialog", { name: "执行隔离设置", exact: true });
  await dialog.getByRole("button", { name: /工作区系统隔离/ }).click();
  await dialog.waitFor({ state: "hidden" });
  await page.getByText("系统隔离 · 工具进程仅可写工作目录，禁止网络", { exact: true }).waitFor();
  assert.equal(await page.getByLabel("访问权限", { exact: true }).inputValue(), "full");
  await send("ISOLATION_TEST 写入测试文件");
  await page.getByText("ISOLATION_DONE", { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
  assert.equal(fs.readFileSync(path.join(project, "isolated-result.md"), "utf8").replace(/\r\n/g, "\n"), "隔离文件工具成功\n");
  await app.close(); app = null; await launch();
  await page.getByText("系统隔离 · 工具进程仅可写工作目录，禁止网络", { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log("Queue/isolation E2E passed: ten accepted, three active/seven queued, automatic replenishment, final waits for all, UI sandbox selection, real LPAC file write, restart restore; hidden local fixture only");
} finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
