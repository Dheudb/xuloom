const { historyRoot } = require("../electron/history-storage.cjs");
const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON, sessionKey } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-session-e2e-"));
const project = path.join(fixture, "项目 one"), second = path.join(fixture, "project-two");
const data = path.join(fixture, "app-data"), config = path.join(fixture, "config");
for (const dir of [project, second, data, config]) fs.mkdirSync(dir);
const legacyHistory = (directory, id) => path.join(directory, ".xuloom", "sessions", id + ".json");
const historyFile = (directory, id) => path.join(historyRoot(directory, data), "sessions", id + ".json");
for (const [directory, id, title] of [[project, "first", "第一条历史"], [project, "second", "第二条历史"],
  [project, "archived", "以前归档的会话"], [second, "other", "另一项目的历史"]])
  atomicJSON(legacyHistory(directory, id), { messages: [{ role: "user", content: title }, { role: "assistant", content: "已有回复" }], metadata: {} });
atomicJSON(path.join(project, ".xuloom/session-index.json"), { version: 1, sessions: { archived: { archived: true } } });
fs.writeFileSync(path.join(project, "keep.py"), "print('keep project source')");
atomicJSON(path.join(data, "workbench.json"), { projects: [project, second], workspace: [{ project, resumeId: "first", active: true }] });
const requests = [];
const server = http.createServer((req, res) => {
  let raw = "";
  req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw); requests.push(body);
    const latest = [...body.messages].reverse().find((message) => message.role === "user")?.content || "";
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({
      id: "memory", model: body.model, choices: [{ index: 0, message: { role: "assistant", content: "memory" }, finish_reason: "stop" }],
    })); return; }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "test", model: body.model,
      object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    chunk({ role: "assistant", content: String(latest).includes("slow-task") ? "正在处理" : "会话连接正常" });
    if (!String(latest).includes("slow-task")) { chunk({}, "stop"); res.end("data: [DONE]\n\n"); }
  });
});
async function main() {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "sessions-test",
    openai_base_url: `http://127.0.0.1:${server.address().port}/v1`, openai_api_key: "fixture-key" });
  const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
  delete env.ELECTRON_RUN_AS_NODE; delete env.XULOOM_DEV_URL;
  for (const key of ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  const options = { executablePath: process.env.XULOOM_TEST_EXECUTABLE,
    args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 };
  let app, page;
  const errors = [];
  async function launch() {
    app = await electron.launch(options); page = await app.firstWindow(); page.setDefaultTimeout(45000);
    page.on("pageerror", (e) => errors.push(e.message));
    await page.locator(".session-item.active").waitFor();
  }
  const row = (title) => page.locator(".session-item").filter({ has: page.locator(".session-select strong", { hasText: title }) });
  async function menu(title) { await row(title).getByRole("button", { name: "会话操作 " + title, exact: true }).click(); }
  async function projectMenu(name) { await page.getByRole("button", { name: "项目操作 " + name, exact: true }).click(); }
  async function newSession() {
    await page.getByRole("button", { name: "在项目 项目 one 中新建会话", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    await page.locator(".session-item.active").filter({ hasText: "新会话" }).waitFor();
  }
  async function activeId() { return page.locator(".session-item.active").getAttribute("data-session-id"); }
  async function screenshot(filename) {
    const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    fs.writeFileSync(path.join(root, "test-results", filename), Buffer.from(png, "base64"));
  }
  try {
    await launch();
    assert.equal(await page.locator(".session-item").count(), 3);
    assert.equal(await row("第一条历史").count(), 1, "Conversation appears exactly once");
    assert.equal(requests.length, 0);
    await menu("第一条历史"); await page.getByRole("menuitem", { name: "重命名会话" }).click();
    await page.getByLabel("新名称", { exact: true }).fill("重命名后的会话");
    await page.getByRole("button", { name: "保存名称" }).click();
    await row("重命名后的会话").waitFor();
    await menu("第二条历史"); await page.getByRole("menuitem", { name: "置顶会话", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".project-sessions .session-item strong")?.textContent === "第二条历史");
    await menu("重命名后的会话"); await page.getByRole("menuitem", { name: "归档会话" }).click();
    await row("重命名后的会话").waitFor({ state: "hidden" });
    assert(fs.existsSync(historyFile(project, "first")));
    assert.equal(await activeId(), sessionKey(project, "second"));
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.getByRole("tab", { name: "已归档会话", exact: true }).click();
    await page.getByRole("button", { name: "恢复会话 重命名后的会话", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已恢复会话" }).waitFor();
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await row("重命名后的会话").locator(".session-select").click();
    await menu("重命名后的会话"); await page.getByRole("menuitem", { name: "复制为新会话" }).click();
    const copyTitle = "重命名后的会话（副本）";
    await page.locator(".session-item.active").filter({ hasText: copyTitle }).waitFor();
    const copyKey = await activeId(), copyNative = copyKey.split(":")[1];
    await page.getByLabel("搜索项目或会话").fill("副本"); assert.equal(await page.locator(".session-item").count(), 1);
    await page.getByLabel("搜索项目或会话").fill("");
    await page.getByLabel("会话排序").selectOption("name");
    await menu(copyTitle); await page.getByRole("menuitem", { name: "删除会话", exact: true }).click();
    assert(fs.existsSync(historyFile(project, copyNative)), "First click only arms delete");
    await page.keyboard.press("Escape"); assert(fs.existsSync(historyFile(project, copyNative)));
    await menu(copyTitle); await page.getByRole("menuitem", { name: "删除会话", exact: true }).click();
    await page.getByRole("menuitem", { name: "确认删除会话", exact: true }).click();
    await row(copyTitle).waitFor({ state: "hidden" });
    assert(!fs.existsSync(historyFile(project, copyNative)));
    assert.notEqual(await activeId(), copyKey);
    // Each "new conversation" click creates a fresh session seeded with the latest explicit preferences
    // (model, reasoning, permission, collaboration); reusing an older empty session would bring back its
    // stale settings. Intentional since 61f44b3 ("conversation defaults").
    await newSession(); const firstEmpty = await activeId();
    await newSession(); const draftKey = await activeId();
    assert.notEqual(draftKey, firstEmpty, "Each new-session click starts a freshly seeded session");
    await page.evaluate(id => window.xuloom.invoke("session.close", { session: id }), firstEmpty);
    const clickNotification = id => app.evaluate(({ BrowserWindow }, session) => {
      BrowserWindow.getAllWindows()[0].webContents.send("workbench:event", { type: "notification_open", session });
    }, id);
    await clickNotification(firstEmpty);
    await page.locator(".session-item.active[data-session-id=" + JSON.stringify(firstEmpty) + "]").waitFor();
    assert(await page.getByRole("textbox", { name: "任务输入" }).isEnabled());
    await clickNotification(draftKey);
    await page.locator(".session-item.active[data-session-id=" + JSON.stringify(draftKey) + "]").waitFor();
    await page.getByRole("textbox", { name: "任务输入" }).fill("未发送的草稿");
    await page.locator(".session-item.active small").filter({ hasText: "有草稿" }).waitFor();
    assert.equal(JSON.parse(fs.readFileSync(path.join(historyRoot(project, data), "index.json"), "utf8")).sessions[draftKey.split(":")[1]].draft, "未发送的草稿");
    await app.close(); await launch();
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "未发送的草稿");
    assert.equal(await page.getByLabel("会话排序").inputValue(), "name");
    assert.equal(await row(copyTitle).count(), 0);
    await page.getByRole("textbox", { name: "任务输入" }).fill("slow-task");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page.getByRole("button", { name: "停止任务" }).waitFor();
    const runningKey = await activeId(), native = runningKey.split(":")[1];
    // Backend refuses a destructive API call that skips confirmation/stopping.
    const refused = await page.evaluate(async (id) => {
      try { await window.xuloom.invoke("session.delete", { id, confirm: true }); return false; } catch { return true; }
    }, runningKey);
    assert(refused);
    await page.getByRole("button", { name: "会话操作 slow-task", exact: true }).click();
    await page.getByRole("menuitem", { name: "停止并删除会话", exact: true }).click();
    await page.getByRole("menuitem", { name: "确认停止并删除", exact: true }).click();
    await page.locator(`[data-session-id="${runningKey}"]`).waitFor({ state: "hidden" });
    assert(!fs.existsSync(historyFile(project, native)));
    await app.close(); await launch();
    assert(!fs.existsSync(historyFile(project, native)), "Disposed engine cannot resurrect deleted history");
    // Project close retains history; removing from sidebar retains history too.
    await projectMenu("project-two"); await page.getByRole("menuitem", { name: "关闭项目" }).click();
    await page.locator(`[data-project="${second.replace(/\\/g, "\\\\")}"] .project-item`).getByText("已关闭").waitFor();
    assert(fs.existsSync(historyFile(second, "other")));
    await projectMenu("project-two"); await page.getByRole("menuitem", { name: "从列表移除项目" }).click();
    await page.getByRole("button", { name: "确认移除项目" }).click();
    await page.getByRole("button", { name: "项目操作 project-two", exact: true }).waitFor({ state: "hidden" });
    assert(fs.existsSync(historyFile(second, "other")));
    // Add example definitions from the real panel; no browser file injection.
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "Skills", exact: true }).click();
    await page.getByRole("button", { name: /Skill 怎么配置/ }).click();
    await page.getByRole("button", { name: "添加示例到当前项目" }).click();
    await page.getByRole("status").filter({ hasText: "已添加 project-review" }).waitFor();
    await page.getByRole("button", { name: "关闭配置说明" }).click();
    await page.getByText("project-review", { exact: true }).waitFor();
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "代理", exact: true }).click();
    await page.getByText("reviewer", { exact: true }).waitFor();
    await page.locator(".capability-card").filter({ has: page.getByText("reviewer", { exact: true }) }).getByRole("button", { name: "查看代理定义" }).click();
    await page.getByRole("dialog", { name: "代理定义" }).getByText(/model: inherit/).waitFor();
    await page.getByRole("button", { name: "关闭代理预览" }).click();
    await screenshot("sessions-management.png");
    // Bulk operations act on the same durable catalog and can be undone by
    // restoring archives, while deletion removes each selected record.
    await page.getByRole("button", { name: "选择多个会话" }).click();
    await page.getByLabel("选择会话 第二条历史", { exact: true }).check();
    await page.getByLabel("选择会话 重命名后的会话", { exact: true }).check();
    await page.getByRole("button", { name: "归档选中会话" }).click();
    await row("第二条历史").waitFor({ state: "hidden" });
    await row("重命名后的会话").waitFor({ state: "hidden" });
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.getByRole("tab", { name: "已归档会话", exact: true }).click();
    for (const name of ["第二条历史", "重命名后的会话"]) {
      await page.getByRole("button", { name: "恢复会话 " + name, exact: true }).click();
      await page.getByRole("status").filter({ hasText: "已恢复会话：" + name }).waitFor();
    }
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page.getByLabel("选择会话 第二条历史", { exact: true }).check();
    await page.getByLabel("选择会话 重命名后的会话", { exact: true }).check();
    await page.getByRole("button", { name: "删除选中会话", exact: true }).click();
    assert(fs.existsSync(historyFile(project, "first")) && fs.existsSync(historyFile(project, "second")));
    await page.getByRole("button", { name: "确认删除选中会话", exact: true }).click();
    await row("第二条历史").waitFor({ state: "hidden" });
    await row("重命名后的会话").waitFor({ state: "hidden" });
    assert(!fs.existsSync(historyFile(project, "first")) && !fs.existsSync(historyFile(project, "second")));
    await page.getByRole("button", { name: "选择多个会话" }).click();
    const projectName = path.basename(project);
    await projectMenu(projectName); await page.getByRole("menuitem", { name: "删除项目会话记录" }).click();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    assert(fs.existsSync(historyFile(project, "archived")));
    await projectMenu(projectName); await page.getByRole("menuitem", { name: "删除项目会话记录" }).click();
    await page.getByRole("button", { name: "确认删除项目会话", exact: true }).click();
    await page.getByRole("button", { name: "项目操作 " + projectName, exact: true }).waitFor({ state: "hidden" });
    assert.equal(fs.readdirSync(path.join(historyRoot(project, data), "sessions")).filter((name) => name.endsWith(".json")).length, 0);
    assert.deepEqual(errors, []);
    assert.equal(fs.readFileSync(path.join(project, "keep.py"), "utf8"), "print('keep project source')");
    console.log("PASS: grouped history, rename, pin, archive/restore, search/sort, fork, two-step delete, empty-slot reuse, persistent draft, restart, stop/delete running engine, project close/remove/delete, batch archive/delete, Skill/Subagent guide and examples");
  } catch (e) {
    if (page && !page.isClosed()) { console.error((await page.locator("body").innerText()).slice(-5000)); await screenshot("sessions-failure.png"); }
    console.error("Fixture: " + fixture); throw e;
  } finally { if (app) await app.close(); server.closeAllConnections(); server.close(); }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
