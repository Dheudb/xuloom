const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const { encodeDSML } = require("./dsml-fixture.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-permissions-e2e-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "profile"), config = path.join(fixture, "config");
for (const directory of [project, data, config]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: project });
const agent = path.join(project, ".xuloom/agents/writer.md");
fs.mkdirSync(path.dirname(agent), { recursive: true });
fs.writeFileSync(agent, "---\nname: writer\ndescription: Permission test writer\ntools: Write, Bash\n---\nPERMISSION_CHILD_PROOF: write the requested test file.");
const textOf = (message) => typeof message.content === "string" ? message.content : (message.content || []).map((block) => block.text || "").join("\n");
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    const sendJSON = () => res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ id: "memory", choices: [{ message: { role: "assistant", content: "memory" }, finish_reason: "stop" }] }));
    if (!body.stream) { sendJSON(); return; }
    // The main agent starts with a lean tool set (agent tools are deferred), so identify the child by its task, not by missing schemas.
    const child = /PERM_CHILD:/.test(textOf(body.messages.find((message) => message.role === "user") || {}));
    const task = [...body.messages].reverse().find((message) => message.role === "user" && /^PERM_(WRITE|SHELL|CHILD):/.test(textOf(message)));
    const match = task ? /^PERM_(WRITE|SHELL|CHILD):([a-z0-9-]+\.txt)/.exec(textOf(task)) : null;
    const taskText = match ? match[0] : "";
    const after = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
    let tool;
    if (match && !after.some((message) => message.role === "tool")) {
      const [_, kind, filename] = match;
      if (kind === "CHILD" && !child) tool = { name: "agent_tool", arguments: { description: "Write test file", prompt: taskText, subagent_type: "writer", run_in_background: false } };
      else if (kind === "SHELL") tool = { name: "bash_tool", arguments: { command: "echo permission-shell > " + filename } };
      else tool = { name: "file_write_tool", arguments: { file_path: path.join(filename === "outside.txt" ? fixture : project, filename), content: "permission-proof" } };
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "test", model: body.model, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (tool && process.env.XULOOM_TEST_DSML === "1") {
      const wire = encodeDSML([{ name: tool.name, input: tool.arguments }]);
      for (let offset = 0; offset < wire.length; offset += 7) chunk({ content: wire.slice(offset, offset + 7) });
      chunk({}, "stop");
    }
    else if (tool) { chunk({ role: "assistant", tool_calls: [{ index: 0, id: "call_" + Date.now(), type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.arguments) } }] }); chunk({}, "tool_calls"); }
    else { chunk({ role: "assistant", content: "权限任务完成：" + taskText }); chunk({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
let app, page, workingDirectory;
const errors = [];
async function launch() {
  const env = fixtureEnv({ data, config, home: path.join(fixture, "home") });
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
  await page.evaluate(() => { window.permissionEvents = []; window.textEvents = []; window.xuloom.onEvent((event) => { if (event.type === "permission") window.permissionEvents.push(event); if (event.type === "text_delta") window.textEvents.push(event.text); }); });
}
const mode = () => page.getByLabel("访问权限", { exact: true });
const row = (id) => page.locator(`[data-session-id="${id}"]`);
const activeId = () => page.locator(".session-item.active").getAttribute("data-session-id");
const permissions = () => page.evaluate(() => window.permissionEvents.length);
async function setMode(value) { await mode().selectOption(value); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled); assert.equal(await mode().inputValue(), value); }
async function send(text) { await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function finished() { await page.waitForFunction(() => !document.querySelector(".send-button.stop")); }
async function verifyWrite(filename, task = "WRITE", outside = false) {
  const before = await permissions(); await send("PERM_" + task + ":" + filename);
  await page.getByText("权限任务完成：PERM_" + task + ":" + filename, { exact: true }).last().waitFor(); await finished();
  assert.equal(await permissions(), before, "Full access must not emit approval requests");
  assert(fs.existsSync(path.join(outside ? fixture : task === "SHELL" ? workingDirectory : project, filename)));
}
async function main() {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "permission-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    const firstId = await activeId();
    workingDirectory = await page.locator(".breadcrumbs button").first().getAttribute("title");
    assert.equal(await mode().inputValue(), "ask");
    assert.equal(await mode().locator("option").count(), 2);
    await send("PERM_WRITE:pending.txt"); await page.getByRole("button", { name: "允许一次", exact: true }).waitFor();
    assert(!fs.existsSync(path.join(project, "pending.txt")));
    await setMode("full");
    await page.getByText("权限任务完成：PERM_WRITE:pending.txt", { exact: true }).last().waitFor();
    await finished();
    assert.equal(fs.readFileSync(path.join(project, "pending.txt"), "utf8"), "permission-proof");
    assert.equal(await page.locator(".interaction").count(), 0);
    await page.getByRole("textbox", { name: "任务输入" }).fill("未发送草稿");
    await setMode("ask"); await setMode("full");
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "未发送草稿");
    await verifyWrite("outside.txt", "WRITE", true);
    await verifyWrite("shell.txt", "SHELL");
    await verifyWrite("child.txt", "CHILD");
    await setMode("ask");
    await send("PERM_WRITE:remembered.txt"); await page.getByRole("button", { name: "本会话允许此工具", exact: true }).click(); await finished();
    await setMode("full"); await setMode("ask");
    await send("PERM_WRITE:revoked.txt"); await page.getByRole("button", { name: "允许一次", exact: true }).waitFor();
    assert(!fs.existsSync(path.join(project, "revoked.txt")), "Returning to approval revokes remembered grants");
    await setMode("full"); await finished(); assert(fs.existsSync(path.join(project, "revoked.txt")));
    await app.close(); await launch();
    assert.equal(await activeId(), firstId);
    assert.equal(await mode().inputValue(), "full");
    await verifyWrite("restart.txt");
    await page.locator(".new-task").click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    const secondId = await activeId(); assert.notEqual(secondId, firstId);
    // New chats inherit the latest explicit access choice (61f44b3); switching back to approval is explicit too.
    assert.equal(await mode().inputValue(), "full"); await setMode("ask");
    await send("PERM_WRITE:denied.txt"); await page.getByRole("button", { name: "拒绝", exact: true }).click(); await finished();
    assert(!fs.existsSync(path.join(project, "denied.txt")));
    await row(firstId).locator(".session-select").click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled); assert.equal(await mode().inputValue(), "full");
    await row(secondId).locator(".session-select").click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled); assert.equal(await mode().inputValue(), "ask");
    // Verify a transport failure cannot leave an invisible full-access runtime.
    await app.evaluate(({ app }) => {
      const localRequire = process.getBuiltinModule("module").createRequire(app.getAppPath() + "/package.json");
      const { EngineTransport } = localRequire("./electron/transport.cjs");
      const original = EngineTransport.prototype.request;
      EngineTransport.prototype.request = function (method, params) {
        if (method === "permissions.set") { EngineTransport.prototype.request = original; return Promise.reject(new Error("fixture failure")); }
        return original.call(this, method, params);
      };
    });
    await mode().selectOption("full");
    await page.getByRole("alert").filter({ hasText: "当前会话引擎已停止" }).waitFor();
    await row(secondId).locator(".session-select").click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    assert.equal(await mode().inputValue(), "ask");
    await send("PERM_WRITE:after-failure.txt"); await page.getByRole("button", { name: "允许一次", exact: true }).waitFor();
    assert(!fs.existsSync(path.join(project, "after-failure.txt")));
    await setMode("full"); await finished();
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/permission-modes.png"), Buffer.from(screenshot, "base64"));
    assert.deepEqual(errors, []);
    assert(!(await page.evaluate(() => window.textEvents.join(""))).includes("DSML"), "Protocol markup must not leak into chat");
    console.log("Permission E2E passed: two modes, pending approval release, full file/shell/subagent execution, revoked grants, per-session isolation, restart and fail-closed transport recovery");
  } catch (error) {
    if (page) {
      console.error("Permission fixture state:", await page.locator("body").innerText().catch(() => ""));
      console.error("Permission fixture requests:", await page.evaluate(() => window.permissionEvents).catch(() => []));
    }
    throw error;
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
