const { historyRoot } = require("../electron/history-storage.cjs");
const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON, sessionKey, projectKey } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-moves-e2e-"));
const first = path.join(fixture, "project-a"), second = path.join(fixture, "项目 b"), data = path.join(fixture, "profile"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const directory of [first, second, data, config, home]) fs.mkdirSync(directory);
const legacyHistory = (directory, id) => path.join(directory, ".xuloom/sessions", id + ".json");
const history = (directory, id) => path.join(historyRoot(directory, data), "sessions", id + ".json");
atomicJSON(legacyHistory(first, "moving"), { messages: [{ role: "user", content: "原来的问题" }, { role: "assistant", content: "原来的回答" }], metadata: {
  memory_files: { cwd: first }, permissions: { always_allow_tools: ["file_write_tool", "bash_tool"] },
  turn_traces: [{ id: "old-turn", status: "completed", elapsedMs: 2345, answer: "原来的回答", steps: [] }],
} });
atomicJSON(legacyHistory(first, "inactive"), { messages: [{ role: "user", content: "未打开的历史" }, { role: "assistant", content: "未打开的回复" }], metadata: {} });
atomicJSON(path.join(first, ".xuloom/session-index.json"), { version: 1, sessions: {
  moving: { title: "迁移中的会话", scope: "standalone", workspaceIsolated: true, draft: "未发送的草稿", pinned: true, permissionMode: "ask" },
} });
for (const [directory, name] of [[first, "only-first"], [second, "only-second"]]) {
  const file = path.join(directory, ".xuloom/skills", name, "SKILL.md"); fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `---\nname: ${name}\ndescription: Project skill ${name}\n---\n${name}`);
  fs.writeFileSync(path.join(directory, "marker.txt"), directory === first ? "FIRST_PROJECT_MARKER" : "SECOND_PROJECT_MARKER");
}
const definition = path.join(second, ".xuloom/agents/location-worker.md"); fs.mkdirSync(path.dirname(definition), { recursive: true });
fs.writeFileSync(definition, "---\nname: location-worker\ndescription: Check current project marker\ntools: Read\n---\nSECOND_WORKER_DEFINITION");
atomicJSON(path.join(second, ".xuloom", "mcp_servers.json"), { mcpServers: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [first, second], standaloneDirectories: [first], defaultDirectory: first,
  workspace: [{ project: first, resumeId: "moving", scope: "standalone", active: true }],
  projectMeta: { [projectKey(second)]: { open: false, collapsed: true } },
});
const requests = [], errors = [];
let app, page;
const textOf = (message) => typeof message.content === "string" ? message.content : (message.content || []).map((block) => block.text || "").join("\n");
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
    requests.push(body);
    const task = [...body.messages].reverse().find((message) => message.role === "user" && /^MOVE_/.test(textOf(message)));
    const taskText = task ? textOf(task) : "";
    const after = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
    const results = after.filter((message) => message.role === "tool");
    const child = textOf(body.messages[0]).includes("SECOND_WORKER_DEFINITION");
    let tool;
    if (!results.length && taskText.startsWith("MOVE_WRITE")) tool = { name: "file_write_tool", arguments: { file_path: "move-proof.txt", content: "TARGET_DIRECTORY_WRITE" } };
    if (!results.length && taskText.startsWith("MOVE_SHELL")) tool = { name: "bash_tool", arguments: { command: "echo TARGET_DIRECTORY_SHELL > shell-proof.txt" } };
    if (!results.length && taskText.startsWith("MOVE_CHILD")) tool = child
      ? { name: "file_read_tool", arguments: { file_path: "marker.txt" } }
      : { name: "agent_tool", arguments: { subagent_type: "location-worker", description: "检查目标项目", prompt: "MOVE_CHILD_READ_MARKER" } };
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (taskText.startsWith("MOVE_SLOW")) { chunk({ role: "assistant", content: "正在运行迁移保护测试" }); return; }
    if (tool) { chunk({ role: "assistant", tool_calls: [{ index: 0, id: "move_" + requests.length, type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.arguments) } }] }); chunk({}, "tool_calls"); }
    else { chunk({ role: "assistant", content: child ? "CHILD_RESULT " + results.map(textOf).join("\n") : taskText + "_DONE" }); chunk({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1", USERPROFILE: home, HOME: home, XULOOM_SKILLS_PATHS: "" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
const row = (id) => page.locator(`[data-session-id="${id}"]`);
const group = (directory) => page.locator(".project-group").filter({ has: page.locator(`.project-item[title="${directory.replaceAll("\\", "\\\\")}"]`) });
async function moved(id, target) {
  await page.waitForFunction((id) => document.querySelector(".session-item.active")?.getAttribute("data-session-id") === id && !document.querySelector(".new-task").disabled, id);
  assert.equal(await group(target).locator(".session-item.active").count(), 1);
}
async function send(task, approval = false) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(task); await page.getByRole("button", { name: "发送任务", exact: true }).click();
  if (approval) await page.getByRole("button", { name: "允许一次", exact: true }).click();
  // A background child makes the model answer again after its completion notice; the earlier answer is kept as
  // progress commentary, so look for the text in the final reply.
  await page.locator(".message-text").getByText(task + "_DONE", { exact: true }).last().waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
async function main() {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "move-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch();
    const old = sessionKey(first, "moving"), next = sessionKey(second, "moving");
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.locator(".capability-card").filter({ has: page.getByText("only-first", { exact: true }) }).getByRole("button", { name: "选择使用", exact: true }).click();
    assert.equal(await page.locator(".selected-skills .selected-skill").count(), 1);
    await page.getByRole("textbox", { name: "任务输入" }).fill("拖动前最后输入的草稿");
    await row(old).dragTo(group(second).locator(".project-heading"));
    await moved(next, second);
    assert.equal(await row(old).count(), 0); assert.equal(await page.locator(".standalone-group .session-item").count(), 0);
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "拖动前最后输入的草稿");
    assert.equal(await page.locator(".selected-skills .selected-skill").count(), 0);
    await page.getByText("原来的回答", { exact: true }).waitFor();
    assert.equal(await page.getByText("only-first", { exact: true }).count(), 0); await page.getByText("only-second", { exact: true }).waitFor();
    assert.equal(await page.getByRole("switch", { name: /启用技能/ }).count(), 0);
    assert(!fs.existsSync(history(first, "moving"))); assert(fs.existsSync(history(second, "moving")));
    await send("MOVE_WRITE", true); // remembered grants from the old workspace must be revoked
    assert.equal(fs.readFileSync(path.join(second, "move-proof.txt"), "utf8"), "TARGET_DIRECTORY_WRITE"); assert(!fs.existsSync(path.join(first, "move-proof.txt")));
    await send("MOVE_SHELL", true); assert(fs.readFileSync(path.join(second, "shell-proof.txt"), "utf8").includes("TARGET_DIRECTORY_SHELL")); assert(!fs.existsSync(path.join(first, "shell-proof.txt")));
    await send("MOVE_CHILD", true);
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "代理", exact: true }).click();
    await page.locator(".worker-result").getByText(/SECOND_PROJECT_MARKER/).waitFor({ state: "attached" });
    const mcp = await page.evaluate((id) => window.xuloom.invoke("mcp.list", { session: id }), next); assert.equal(projectKey(mcp.project), projectKey(second));
    // Windows paths compare case-insensitively (TEMP may be spelled C:\WINDOWS\TEMP while Python reports C:\Windows\Temp).
    const samePath = (text) => process.platform === "win32" ? text.toLowerCase() : text;
    assert(requests.some((request) => textOf(request.messages[0]).includes("Conversation working directory") && samePath(textOf(request.messages[0])).includes(samePath(fs.realpathSync(second)))));
    // Running tasks cannot change directory under a live tool call.
    await page.getByRole("textbox", { name: "任务输入" }).fill("MOVE_SLOW"); await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByText("正在运行迁移保护测试", { exact: true }).waitFor(); assert.equal(await row(next).getAttribute("draggable"), "false");
    const blocked = await page.evaluate(async ({ id, path }) => { try { await window.xuloom.invoke("session.move", { id, path }); return ""; } catch (error) { return String(error); } }, { id: next, path: first });
    assert.match(blocked, /先停止/); assert(fs.existsSync(history(second, "moving"))); assert(!fs.existsSync(history(first, "moving")));
    await page.getByRole("button", { name: "停止任务", exact: true }).click(); await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    // Keyboard-accessible menu path also moves an existing project conversation.
    await row(next).getByRole("button", { name: "会话操作 迁移中的会话", exact: true }).click();
    await page.getByRole("menuitem", { name: "移动到项目…", exact: true }).click();
    await page.getByLabel("目标项目", { exact: true }).selectOption(first); await page.getByRole("button", { name: "移动会话", exact: true }).click();
    await moved(old, first); assert(fs.existsSync(history(first, "moving"))); assert(!fs.existsSync(history(second, "moving")));
    assert(fs.existsSync(path.join(second, "move-proof.txt")), "Project files stay in their original project");
    // Move a history that was never opened in this app process.
    await row(sessionKey(first, "inactive")).dragTo(group(second).locator(".project-heading"));
    await moved(sessionKey(second, "inactive"), second); await page.getByText("未打开的回复", { exact: true }).waitFor();
    await app.close(); await launch(); await moved(sessionKey(second, "inactive"), second);
    await page.getByText("未打开的回复", { exact: true }).waitFor(); assert(!fs.existsSync(history(first, "inactive")));
    // A standalone chat owns a private directory; moving changes its association and working directory.
    await page.locator(".new-task").click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    const same = await page.locator(".session-item.active").getAttribute("data-session-id");
    await page.getByRole("textbox", { name: "任务输入" }).fill("SAME_DIRECTORY_DRAFT");
    await row(same).dragTo(group(first).locator(".project-heading")); await moved(sessionKey(first, same.split(":")[1]), first);
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "SAME_DIRECTORY_DRAFT");
    const shot = await app.evaluate(async ({ BrowserWindow }) => {
      const web = BrowserWindow.getAllWindows()[0].webContents; await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
      return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/session-move.png"), Buffer.from(shot, "base64"));
    assert.deepEqual(errors, []);
    console.log("Move E2E passed: native drag/drop, closed destination, drafts/history, actual file/shell/child cwd, new Skills/MCP, revoked old grants, busy rejection, menu fallback, unopened history, restart and same-directory conversion");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
