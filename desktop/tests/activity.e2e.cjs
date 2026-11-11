const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-activity-e2e-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "profile"), config = path.join(fixture, "config");
for (const directory of [project, data, config]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: project });
let app, page;
const errors = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
    // The main agent starts with a lean tool set (agent tools are deferred), so identify the child by its task, not by missing schemas.
    const first = body.messages.find((message) => message.role === "user");
    const child = JSON.stringify(first?.content || "").includes("ACTIVITY_CHILD");
    const task = [...body.messages].reverse().find((message) => message.role === "user" && typeof message.content === "string" && message.content.startsWith("ACTIVITY_"));
    const after = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
    let tools;
    if (task && !after.some((message) => message.role === "tool")) {
      if (child) tools = [{ id: "call_shell", name: "bash_tool", input: { command: "echo ACTIVITY_CHILD_OUTPUT", cwd: project } }];
      else if (task.content === "ACTIVITY_SUCCESS") tools = [
        { id: "call_write", name: "file_write_tool", input: { file_path: path.join(project, "proof.txt"), content: "ACTIVITY_FILE_PROOF" } },
        { id: "call_missing", name: "file_read_tool", input: { file_path: path.join(project, "missing.txt") } },
        { id: "call_shell", name: "bash_tool", input: { command: "echo ACTIVITY_PARENT_OUTPUT", cwd: project } },
        { id: "call_agent", name: "agent_tool", input: { description: "独立检查", prompt: "ACTIVITY_CHILD", subagent_type: "general-purpose" } },
      ];
      else tools = [{ id: task.content, name: "file_write_tool", input: { file_path: path.join(project, task.content + ".txt"), content: "must need approval" } }];
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (tools) { chunk({ role: "assistant", tool_calls: tools.map((tool, index) => ({ index, id: tool.id, type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.input) } })) }); chunk({}, "tool_calls"); }
    else { chunk({ role: "assistant", content: child ? "独立检查已完成" : "本轮活动测试完成" }); chunk({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = fixtureEnv({ data, config, home: path.join(fixture, "home") });
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
  await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
}
const separator = () => page.getByRole("separator", { name: "调整右侧面板宽度" });
const settings = () => JSON.parse(fs.readFileSync(path.join(data, "workbench.json"), "utf8"));
async function send(text) { await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function finished() { await page.waitForFunction(() => !document.querySelector(".send-button.stop")); }
async function setMode(value) { await page.getByLabel("访问权限", { exact: true }).selectOption(value); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled); }
async function dragWidth(target) {
  const before = Number(await separator().getAttribute("aria-valuenow"));
  const box = await separator().boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + 110); await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - (target - before), box.y + 110, { steps: 10 }); await page.mouse.up();
  await page.waitForFunction((target) => Number(document.querySelector('.inspector-shell > [role="separator"]').getAttribute("aria-valuenow")) === target, target);
  assert.equal(settings().layout.inspectorWidth, target);
}
async function main() {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "activity-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor(); await setMode("full");
    await page.getByRole("button", { name: "活动", exact: true }).click();
    await send("ACTIVITY_SUCCESS");
    await page.waitForFunction(() => document.querySelectorAll(".activity-card").length === 6 && !document.querySelector('.activity-card[data-status="running"],.activity-card[data-status="queued"]'));
    await finished(); assert.equal(await page.locator('.activity-card[data-status="error"]').count(), 1);
    const shell = page.locator('[data-activity-id="main:call_shell"]');
    await shell.locator("summary").click(); assert((await shell.innerText()).includes("ACTIVITY_PARENT_OUTPUT"));
    assert((await shell.locator(".activity-detail").innerText()).includes('"command"'));
    await page.getByLabel("活动代理范围").selectOption("children");
    assert.equal(await page.locator(".activity-card").count(), 2);
    assert((await page.locator(".activity-scroll").innerText()).includes("ACTIVITY_CHILD_OUTPUT"));
    await page.getByLabel("活动代理范围").selectOption("all");
    await page.getByLabel("搜索执行活动").fill("missing.txt"); assert.equal(await page.locator(".activity-card").count(), 1); await page.getByLabel("搜索执行活动").fill("");
    await dragWidth(540);
    await page.getByRole("button", { name: "收起能力面板" }).click(); assert.equal(await separator().count(), 0);
    await page.getByRole("button", { name: "展开能力面板" }).click(); assert.equal(await separator().getAttribute("aria-valuenow"), "540");
    await separator().focus(); await page.keyboard.press("ArrowLeft"); assert.equal(await separator().getAttribute("aria-valuenow"), "564");
    await separator().dblclick(); assert.equal(await separator().getAttribute("aria-valuenow"), "318"); await dragWidth(560);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1050, 820));
    await page.waitForFunction(() => innerWidth <= 1060 && document.querySelector(".main-panel").getBoundingClientRect().width >= 399 && document.querySelector(".inspector-shell").getBoundingClientRect().width <= innerWidth - document.querySelector(".sidebar").getBoundingClientRect().width - 399);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1600, 1000));
    await page.waitForFunction(() => document.querySelector(".inspector-shell").getBoundingClientRect().width === 560);
    await setMode("ask"); await send("ACTIVITY_DENY");
    await page.locator('[data-activity-id="main:ACTIVITY_DENY"][data-status="waiting"]').waitFor();
    await page.getByRole("button", { name: "拒绝", exact: true }).click(); await finished();
    assert.equal(await page.locator('[data-activity-id="main:ACTIVITY_DENY"]').getAttribute("data-status"), "error");
    await send("ACTIVITY_CANCEL"); await page.getByRole("button", { name: "允许一次", exact: true }).waitFor();
    await page.getByRole("button", { name: "停止任务", exact: true }).click(); await finished();
    assert.equal(await page.locator('[data-activity-id="main:ACTIVITY_CANCEL"]').getAttribute("data-status"), "cancelled");
    assert(!fs.existsSync(path.join(project, "ACTIVITY_CANCEL.txt")));
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/activity.png"), Buffer.from(screenshot, "base64"));
    await app.close(); await launch(); assert.equal(await separator().getAttribute("aria-valuenow"), "560");
    await page.getByRole("button", { name: "活动", exact: true }).click();
    assert.equal(await page.locator('[data-activity-id="main:call_shell"]').getAttribute("data-status"), "completed");
    assert((await page.locator('[data-activity-id="main:call_shell"]').innerText()).includes("ACTIVITY_PARENT_OUTPUT"));
    await page.getByLabel("活动代理范围").selectOption("children"); assert.equal(await page.locator(".activity-card").count(), 2);
    assert.deepEqual(errors, []);
    console.log("Activity E2E passed: real tool parameters/results/errors, child scopes, approval/denial/cancel states, history restoration, drag/keyboard/reset, width persistence and narrow-window bounds");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
