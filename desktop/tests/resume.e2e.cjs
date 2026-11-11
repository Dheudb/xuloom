const { _electron: electron } = require("playwright");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-resume-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null });
let app, page, requests = [], failures = [], resumedRequests = 0;
let zeroStarted;
const zeroReady = new Promise(resolve => { zeroStarted = resolve; });
const textOf = (m) => typeof m.content === "string" ? m.content : (m.content || []).map((b) => b.text || "").join("\n");
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk); req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
    requests.push(body);
    const task = [...body.messages].reverse().find((m) => m.role === "user");
    const text = textOf(task);
    const continuing = text.includes("[Task resumed by user]");
    const zeroContinuing = continuing && body.messages.some(m => m.role === "user" && textOf(m).startsWith("ZERO_STOP"));
    if (continuing) {
      resumedRequests++;
      if (!body.messages.some((m) => m.role === "user" && textOf(m) === "STOP_ORIGINAL")) failures.push("Original request lost on resume");
      if (!zeroContinuing && !body.messages.some((m) => m.role === "assistant" && textOf(m).includes("PARTIAL_PROGRESS"))) failures.push("Partial progress lost on resume");
    }
    const system = body.messages.filter((m) => m.role === "system").map(textOf).join("\n");
    if (system.includes("resume-fixture") || system.includes("Configured request model ID:")) failures.push("Model routing leaked into resumed identity prompt");
    // The forced self-introduction prompt was removed in 8e13e4c; only check that routing does not leak.
    if (text === "EDITED_TASK" && body.messages.some((m) => m.role === "user" && textOf(m) === "EDIT_ORIGINAL")) failures.push("Superseded prompt still sent to model");
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "test", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (["STOP_ORIGINAL", "EDIT_ORIGINAL", "APPROVAL_STOP", "ZERO_STOP"].includes(text) || continuing && resumedRequests === 1) {
      if (text === "APPROVAL_STOP") chunk({ role: "assistant", tool_calls: [{ index: 0, id: "permission-test", type: "function", function: { name: "file_write_tool", arguments: JSON.stringify({ file_path: "denied.md", content: "never" }) } }] });
      else if (text === "ZERO_STOP") zeroStarted();
      else chunk({ role: "assistant", content: continuing ? "NEXT_PROGRESS" : "PARTIAL_PROGRESS" });
      if (text === "APPROVAL_STOP") { chunk({}, "tool_calls"); res.end("data: [DONE]\n\n"); }
      else { const heartbeat = setInterval(() => res.write(": waiting\n\n"), 200); res.on("close", () => clearInterval(heartbeat)); }
    } else { chunk({ role: "assistant", content: zeroContinuing ? "ZERO_DONE" : continuing ? "RESUMED_DONE" : text + "_DONE" }); chunk({}, "stop"); res.end("data: [DONE]\n\n"); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, HOME: home, USERPROFILE: home, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(90000); page.on("pageerror", (e) => failures.push(e.message));
  await app.evaluate(({ app }) => {
    globalThis.resumeTransportTrace = [];
    const localRequire = process.getBuiltinModule("module").createRequire(app.getAppPath() + "/package.json");
    const { EngineTransport } = localRequire("./electron/transport.cjs");
    const original = EngineTransport.prototype.request;
    EngineTransport.prototype.request = async function (method, params) {
      const row = { method, text: params?.text, at: Date.now() };
      globalThis.resumeTransportTrace.push(row);
      try { const value = await original.call(this, method, params); row.ok = true; return value; }
      catch (error) { row.error = String(error); throw error; }
    };
  });
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
const input = () => page.getByRole("textbox", { name: "任务输入", exact: true });
async function send(text) { await input().fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function stop() {
  await page.getByRole("button", { name: "停止任务", exact: true }).click();
  await page.getByRole("button", { name: "继续执行", exact: true }).waitFor();
  assert.equal(await page.locator(".paused-actions").count(), 0);
  assert.equal(await page.locator(".composer .send-button.resume").count(), 1);
  assert(await input().isEnabled());
}
async function capture(name) {
  const png = await app.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
  fs.writeFileSync(path.join(root, "test-results", name), Buffer.from(png, "base64"));
}
(async () => {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "resume-fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click();
    await send("STOP_ORIGINAL"); await page.getByText("PARTIAL_PROGRESS", { exact: true }).last().waitFor(); await stop();
    await page.getByText("PARTIAL_PROGRESS", { exact: true }).last().waitFor();
    const id = await page.locator(".session-item.active").getAttribute("data-session-id");
    const cwd = await page.locator(".breadcrumbs button").getAttribute("title");
    assert(cwd && path.isAbsolute(cwd));
    await app.close(); await launch();
    await page.getByText("PARTIAL_PROGRESS", { exact: true }).last().waitFor();
    assert.equal(await page.locator(".message.user").count(), 1);
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    await capture("resume-stopped.png");
    await input().fill("unsent draft");
    assert.equal(await page.getByRole("button", { name: "继续执行", exact: true }).count(), 0);
    assert(await page.getByRole("button", { name: "发送任务", exact: true }).isEnabled());
    await input().fill("");
    await page.getByRole("button", { name: "继续执行", exact: true }).click();
    await page.getByText("PARTIAL_PROGRESSNEXT_PROGRESS", { exact: true }).waitFor();
    await stop();
    await page.getByRole("button", { name: "继续执行", exact: true }).click();
    await page.getByText("PARTIAL_PROGRESSNEXT_PROGRESSRESUMED_DONE", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(await page.locator(".message.user").count(), 1);
    assert.equal(await page.locator(".message.assistant").count(), 1);
    assert.equal(await page.locator(".turn-process").count(), 1);
    await app.close(); await launch();
    await page.getByText("PARTIAL_PROGRESSNEXT_PROGRESSRESUMED_DONE", { exact: true }).waitFor();
    assert.equal(await page.locator(".message.user").count(), 1);
    assert.equal(await page.locator(".message.assistant").count(), 1);
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    await capture("resume-completed.png");
    await send("EDIT_ORIGINAL"); await page.getByText("PARTIAL_PROGRESS", { exact: true }).last().waitFor(); await stop();
    await input().focus(); await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
    assert.equal(await input().inputValue(), "EDIT_ORIGINAL");
    await page.getByRole("button", { name: "取消编辑提示词", exact: true }).click();
    await page.getByRole("button", { name: "编辑上一条提示词", exact: true }).click(); assert.equal(await input().inputValue(), "EDIT_ORIGINAL");
    await input().fill("EDITED_TASK");
    await page.waitForFunction(() => document.querySelector(".session-select small")?.textContent === "有草稿");
    await app.close(); await launch();
    assert.equal(await input().inputValue(), "EDITED_TASK"); await page.getByRole("button", { name: "取消编辑提示词", exact: true }).waitFor();
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByText("EDITED_TASK_DONE", { exact: true }).waitFor(); await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(await page.locator(".session-item.active").getAttribute("data-session-id"), id);
    assert.equal(await page.locator(".breadcrumbs button").getAttribute("title"), cwd);
    assert.equal(await page.locator(".message.user").filter({ hasText: "EDIT_ORIGINAL" }).count(), 0);
    const recovery = (directory) => fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? recovery(path.join(directory, entry.name)) : [path.join(directory, entry.name)]);
    assert(recovery(data).some((file) => file.includes(path.sep + "revisions" + path.sep) && fs.readFileSync(file, "utf8").includes("EDIT_ORIGINAL")));
    const humansBeforeZero = await page.locator(".message.user").count();
    await send("ZERO_STOP"); await zeroReady; await stop();
    await capture("resume-empty.png");
    await page.getByRole("button", { name: "继续执行", exact: true }).click();
    await page.getByText("ZERO_DONE", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(await page.locator(".message.user").count(), humansBeforeZero + 1);
    await send("APPROVAL_STOP"); await page.getByRole("button", { name: "拒绝", exact: true }).waitFor(); await stop();
    assert.equal(await page.getByRole("button", { name: "拒绝", exact: true }).count(), 0);
    assert.deepEqual(failures, []);
    console.log("Resume E2E passed: composer play button, restart and repeated stop/resume append to one response, no synthetic user turns, draft/send switch, inline prompt edit and approval cancellation.");
  } catch (error) {
    if (page) console.error("Resume fixture UI errors:", await page.locator(".session-error, .global-notice").allTextContents().catch(() => []));
    if (page) console.error("Resume fixture UI state:", await page.locator("body").innerText().catch(() => ""));
    console.error("Resume fixture tasks:", requests.map((body) => textOf([...body.messages].reverse().find((message) => message.role === "user") || {})));
    if (app) console.error("Resume fixture transport:", await app.evaluate(() => globalThis.resumeTransportTrace).catch(() => []));
    throw error;
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
