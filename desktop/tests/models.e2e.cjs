const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-models-e2e-"));
const data = path.join(fixture, "profile"), config = path.join(fixture, "config"), project = path.join(fixture, "project");
for (const directory of [data, config, project]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: project });
const requests = [];
function server(name, key, models) {
  return http.createServer((req, res) => {
    assert.equal(req.headers.authorization, "Bearer " + key, "API credentials must stay with their connection");
    if (req.url === "/v1/models") {
      requests.push({ server: name, list: true });
      res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ data: models.map((id) => ({ id, type: "chat" })) })); return;
    }
    let raw = ""; req.on("data", (chunk) => raw += chunk);
    req.on("end", () => {
      const body = JSON.parse(raw); requests.push({ server: name, body });
      const last = [...body.messages].reverse().find((message) => message.role === "user")?.content || "";
      if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ id: "memory", choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
      const system = body.messages.filter((message) => message.role === "system").map((message) => message.content).join("\n");
      assert(!system.includes("Configured request model ID:"), "Routing model IDs must not be injected into identity instructions");
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "test", model: body.model, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
      chunk({ role: "assistant", content: "模型回复：" + body.model });
      if (!String(last).trim().endsWith("slow-task")) { chunk({}, "stop"); res.end("data: [DONE]\n\n"); }
    });
  });
}
const first = server("first", "first-fixture-key", ["one", "two", "three"]), second = server("second", "second-fixture-key", ["other", "another"]);
let app, page;
const errors = [];
async function launch() {
  const env = fixtureEnv({ data, config, home: path.join(fixture, "home") });
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
  await page.addStyleTag({ content: "*, *::before, *::after { animation: none !important; transition: none !important; }" });
}
const readConfig = () => JSON.parse(fs.readFileSync(path.join(config, "config.json"), "utf8"));
async function settings() {
  await page.getByRole("button", { name: "设置与模型", exact: true }).click();
  await page.locator('.model-options > summary').click();
  await page.getByRole('button', { name: '管理模型', exact: true }).click();
}
async function save() { await page.getByRole("button", { name: "保存配置", exact: true }).click(); await page.getByRole("dialog", { name: "工作台设置" }).waitFor({ state: "hidden" }); }
async function select(connection, model) {
  await page.getByLabel("选择模型", { exact: true }).selectOption(JSON.stringify([connection, model]));
  await page.waitForFunction(() => !document.querySelector(".model-picker").disabled);
}
async function send(text, model) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.getByText("模型回复：" + model, { exact: true }).last().waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
async function confirmDelete() {
  await page.getByRole("button", { name: "删除 API", exact: true }).click();
  await page.getByRole("button", { name: "确认删除 API", exact: true }).click();
  await page.getByRole("status").filter({ hasText: /已删除/ }).waitFor();
}
async function main() {
  try {
    await Promise.all([new Promise((resolve) => first.listen(0, "127.0.0.1", resolve)), new Promise((resolve) => second.listen(0, "127.0.0.1", resolve))]);
    const firstUrl = `http://127.0.0.1:${first.address().port}/v1`, secondUrl = `http://127.0.0.1:${second.address().port}/v1`;
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "one", openai_base_url: firstUrl, openai_api_key: "first-fixture-key" });
    await launch(); await settings();
    await page.getByLabel("连接名称", { exact: true }).fill("第一个 API");
    await page.getByLabel("模型输入能力", { exact: true }).selectOption("text");
    await page.getByLabel("可选模型列表", { exact: true }).fill("one\ntwo\ntwo"); await save();
    await settings();
    const beforeSearch = requests.length;
    await page.getByLabel('联网搜索', { exact: true }).selectOption('deepseek');
    await page.getByText('搜索接口地址', { exact: true }).click();
    await page.getByLabel('搜索 API 根地址', { exact: true }).fill(firstUrl);
    await save();
    assert.equal(requests.length, beforeSearch, 'Saving native search never calls a provider');
    assert.deepEqual(readConfig().model_connections[0].native_search, { backend: 'deepseek', model: '', baseUrl: firstUrl });
    await settings();
    assert.equal(await page.getByLabel('联网搜索', { exact: true }).inputValue(), 'deepseek');
    await page.getByLabel('联网搜索', { exact: true }).selectOption('builtin'); await save();
    assert.deepEqual(readConfig().openai_models, ["one", "two"]);
    assert.deepEqual(readConfig().model_connections[0].model_inputs.one, ["text"]);
    await settings();
    const unchanged = fs.readFileSync(path.join(config, "config.json"), "utf8");
    await page.getByRole("button", { name: "从 API 获取模型", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已读取 3 个模型" }).waitFor();
    assert.equal(await page.getByLabel("可选模型列表").inputValue(), "one\ntwo\nthree");
    assert.equal(fs.readFileSync(path.join(config, "config.json"), "utf8"), unchanged, "Discovery does not save or change the active model");
    await save();
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    const session = await page.locator(".session-item.active").getAttribute("data-session-id");
    await send("第一次消息", "one");
    await page.getByRole("textbox", { name: "任务输入" }).fill("保留草稿");
    await select("legacy-openai", "two");
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "保留草稿");
    assert.equal(await page.locator(".session-item.active").getAttribute("data-session-id"), session);
    await send("第二次消息", "two");
    const two = requests.find((request) => request.body?.model === "two");
    assert(two.body.messages.some((message) => message.role === "user" && message.content === "第一次消息"));
    await settings(); await page.getByRole("button", { name: "新增 API", exact: true }).click();
    await page.getByLabel("连接名称", { exact: true }).fill("第二个 API");
    await page.getByLabel("API 地址", { exact: true }).fill(secondUrl);
    await page.getByLabel("API Key", { exact: true }).fill("second-fixture-key");
    await page.getByRole("button", { name: "从 API 获取模型", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已读取 2 个模型" }).waitFor();
    await save();
    const secondId = readConfig().active_model_connection;
    assert.equal(readConfig().model_connections.length, 2);
    await send("切到另一个 API", "other");
    assert(requests.some((request) => request.server === "second" && request.body?.model === "other"));
    const publicConfig = await page.evaluate(() => window.xuloom.invoke("config.get"));
    assert(!JSON.stringify(publicConfig).includes("fixture-key"));
    await select("legacy-openai", "three");
    await send("返回第一个 API", "three");
    await page.getByRole("textbox", { name: "任务输入" }).fill("slow-task");
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByRole("button", { name: "停止任务", exact: true }).waitFor();
    assert(await page.getByLabel("选择模型", { exact: true }).isDisabled());
    const refused = await page.evaluate(async ([session, connectionId]) => {
      try { await window.xuloom.invoke("config.select", { session, connectionId, model: "another" }); return false; } catch { return true; }
    }, [session, secondId]);
    assert(refused, "Switching a running session must be refused");
    const beforeDelete = fs.readFileSync(path.join(config, "config.json"), "utf8");
    assert(await page.evaluate(async (connectionId) => {
      try { await window.xuloom.invoke("config.delete", { connectionId, confirm: true }); return false; } catch { return true; }
    }, secondId));
    assert.equal(fs.readFileSync(path.join(config, "config.json"), "utf8"), beforeDelete);
    await page.getByRole("button", { name: "停止任务", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".model-picker").disabled);
    await select(secondId, "another");
    await send("停止后切换", "another");
    await app.close(); await launch();
    assert.equal(await page.getByLabel("选择模型", { exact: true }).inputValue(), JSON.stringify([secondId, "another"]));
    assert.equal(await page.locator(".session-item.active").getAttribute("data-session-id"), session);
    await settings();
    assert.equal(await page.getByLabel("可选模型列表", { exact: true }).inputValue(), "other\nanother");
    assert.equal(await page.getByLabel("API Key", { exact: true }).inputValue(), "");
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => {
      const contents = BrowserWindow.getAllWindows()[0].webContents;
      contents.invalidate();
      return (await contents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.writeFileSync(path.join(root, "test-results/models-settings.png"), Buffer.from(screenshot, "base64"));
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await send("重启后继续", "another");
    await settings();
    const beforeCancel = fs.readFileSync(path.join(config, "config.json"), "utf8");
    await page.getByRole("button", { name: "删除 API", exact: true }).click();
    await page.getByRole("button", { name: "保留 API", exact: true }).click();
    assert.equal(fs.readFileSync(path.join(config, "config.json"), "utf8"), beforeCancel);
    assert(await page.evaluate(async (connectionId) => {
      try { await window.xuloom.invoke("config.delete", { connectionId }); return false; } catch { return true; }
    }, secondId), "Backend requires explicit deletion confirmation");
    await confirmDelete();
    assert.equal(readConfig().active_model_connection, "legacy-openai");
    if (process.platform === "win32") {
      assert.equal(readConfig().openai_api_key.$xuloom_secret, "dpapi-v1");
      assert(!JSON.stringify(readConfig()).includes("first-fixture-key"), "Saved keys must be protected at rest");
    } else assert.equal(readConfig().openai_api_key, "first-fixture-key");
    assert(!JSON.stringify(readConfig()).includes("second-fixture-key"));
    assert.equal(await page.getByLabel("API 连接").inputValue(), "legacy-openai");
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    assert.equal(await page.locator(".session-item.active").getAttribute("data-session-id"), session);
    await send("删除当前 API 后继续", "three");
    await settings();
    await confirmDelete();
    assert.deepEqual(readConfig().model_connections, []);
    assert(!JSON.stringify(readConfig()).includes("fixture-key"));
    assert.equal(await page.getByLabel("API Key", { exact: true }).inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "删除 API", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    const requestCount = requests.length;
    await page.getByRole("textbox", { name: "任务输入" }).fill("未配置时不能请求旧 API");
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByText(/Model not configured/).last().waitFor();
    assert.equal(requests.length, requestCount);
    await app.close(); await launch();
    const empty = await page.evaluate(() => window.xuloom.invoke("config.get"));
    assert.deepEqual(empty.connections, []); assert.equal(empty.model, ""); assert.equal(empty.hasKey, false);
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await settings(); await page.getByRole("button", { name: "新增 API", exact: true }).click();
    await page.getByLabel("连接名称", { exact: true }).fill("重新添加的 API");
    await page.getByLabel("模型 ID", { exact: true }).fill("one");
    await page.getByLabel("API 地址", { exact: true }).fill(firstUrl);
    await page.getByLabel("API Key", { exact: true }).fill("first-fixture-key"); await save();
    await send("重新添加后正常请求", "one");
    if (process.platform === "win32") {
      await app.close();
      const damaged = readConfig();
      damaged.model_connections.find(row => row.id === damaged.active_model_connection).api_key.data = "unreadable-owned-fixture!";
      damaged.openai_api_key.data = "unreadable-owned-fixture!";
      atomicJSON(path.join(config, "config.json"), damaged);
      const beforeRepair = requests.length;
      await launch(); await settings();
      await page.getByRole('alert').filter({ hasText: /Key.*无法读取/ }).waitFor();
      assert.equal(await page.getByLabel("API 地址", { exact: true }).inputValue(), firstUrl);
      assert.equal(requests.length, beforeRepair, "Unreadable credentials do not issue requests during restart");
      await page.getByLabel("API Key", { exact: true }).fill("first-fixture-key"); await save();
      await send("修复凭据后继续", "one");
      assert(!JSON.stringify(readConfig()).includes("first-fixture-key"));
    }
    assert.deepEqual(errors, []);
    console.log("Model E2E passed: shared keys/models, switching, running guard, confirmed deletion, active fallback, empty catalog restart, no stale API requests and re-add");
  } finally {
    if (app) await app.close().catch(() => {});
    first.closeAllConnections(); first.close(); second.closeAllConnections(); second.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
