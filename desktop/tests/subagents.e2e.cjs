const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const { encodeDSML } = require("./dsml-fixture.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-subagents-e2e-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "profile"), config = path.join(fixture, "config");
for (const directory of [project, data, config]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: project });
const childRequests = [], pending = [], failures = [];
let app, page, batch = 0;
function reply(res, text, tools) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
  if (tools && process.env.XULOOM_TEST_DSML === "1") {
    const wire = encodeDSML(tools.map((input) => ({ name: "agent_tool", input })));
    for (let offset = 0; offset < wire.length; offset += 17) chunk({ content: wire.slice(offset, offset + 17) });
    chunk({}, "stop");
  } else if (tools) {
    chunk({ role: "assistant", tool_calls: tools.map((arguments_, index) => ({ index, id: `call_${batch}_${index}`, type: "function", function: { name: "agent_tool", arguments: JSON.stringify(arguments_) } })) });
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
      const child = !(body.tools || []).some((tool) => tool.function?.name === "agent_tool");
      if (child) {
        childRequests.push(body);
        const users = body.messages.filter((message) => message.role === "user");
        assert.deepEqual(users.map((message) => message.content), ["你好"], "Child must receive only its own assignment");
        assert(body.messages.some((message) => message.role === "system" && message.content.includes("not the parent coordinator")));
        assert(!JSON.stringify(body.messages).includes("启动10个"), "Parent delegation request must not leak into child context");
        pending.push(res);
        // A serial dispatcher deadlocks here: all ten must enter before any is released.
        if (pending.length === 10) {
          pending.splice(0).forEach((response, index) => reply(response, `你好，子代理 ${batch}-${index + 1} 已完成`));
        }
        return;
      }
      const task = [...body.messages].reverse().find((message) => message.role === "user");
      const afterTask = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
      if (typeof task?.content === "string" && task.content.startsWith("启动10个") && !afterTask.some((message) => message.role === "tool")) {
        batch++;
        const schema = body.tools.find((tool) => tool.function?.name === "agent_tool").function.parameters;
        assert.equal(schema.properties.count.maximum, 10);
        const background = task.content.includes("后台");
        reply(res, null, task.content.includes("单次")
          ? [{ description: `问候测试 ${batch}`, prompt: "你好", count: 10, subagent_type: "general-purpose", run_in_background: background }]
          : Array.from({ length: 10 }, (_, index) => ({ description: `问候测试 ${batch}-${index + 1}`, prompt: "你好", subagent_type: "general-purpose", run_in_background: background })));
      } else {
        reply(res, "已启动本批 10 个子代理");
      }
    } catch (error) { failures.push(error.message); res.writeHead(500).end("fixture rejected child context"); }
  });
});
async function main() {
  try {
    // Explicit host override tests the transport maximum; product defaults are 3/8.
    atomicJSON(path.join(project, ".xuloom", "capabilities.json"), { agent_concurrency: 10 });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "subagent-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
    for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
    page = await app.firstWindow(); page.setDefaultTimeout(60000);
    page.on("pageerror", (error) => failures.push(error.message));
    await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await page.evaluate(() => { window.workerEvents = []; window.xuloom.onEvent((event) => { if (event.type === "subagent_status") window.workerEvents.push(event.agent); }); });
    for (const [index, task] of ["启动10个subagent，都发送你好", "启动10个后台subagent，都发送你好", "启动10个subagent，单次批量，都发送你好", "启动10个后台subagent，单次批量，都发送你好"].entries()) {
      await page.getByRole("textbox", { name: "任务输入" }).fill(task);
      await page.getByRole("button", { name: "发送任务", exact: true }).click();
      await page.waitForFunction((count) => window.workerEvents.filter((worker) => worker.status === "completed").length === count, (index + 1) * 10);
      await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
      assert.equal(childRequests.length, (index + 1) * 10);
    }
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "代理", exact: true }).click();
    assert.equal(await page.locator(".capability-card").filter({ hasText: "问候测试" }).count(), 40);
    assert.equal(await page.locator(".capability-card").filter({ hasText: "已完成" }).count(), 40);
    assert.equal(await page.locator(".capability-card").filter({ hasText: "问候测试 4 10/10" }).count(), 1);
    assert.equal(await page.locator(".capability-card").filter({ hasText: "启动 " }).count(), 40);
    const historyButton = page.getByRole("button", { name: "查看完整对话历史", exact: true }).first();
    await historyButton.click();
    const history = page.getByRole("dialog", { name: "子代理完整历史", exact: true });
    await history.getByText("已到当前快照末尾", { exact: true }).waitFor();
    assert((await history.locator(".history-entry").count()) >= 2);
    const requestsBeforeRead = childRequests.length;
    await history.getByLabel("复制此条历史", { exact: true }).last().click();
    assert((await app.evaluate(({ clipboard }) => clipboard.readText())).includes("你好"));
    await history.evaluate(node => [...node.querySelectorAll('button:not([disabled]), a[href], summary, [tabindex="0"]')].at(-1).focus());
    await page.keyboard.press("Tab");
    assert.equal(await history.getByLabel("刷新历史快照", { exact: true }).evaluate(node => node === document.activeElement), true);
    await page.keyboard.press("Escape");
    await history.waitFor({ state: "detached" });
    assert.equal(await historyButton.evaluate(node => node === document.activeElement), true);
    assert.equal(childRequests.length, requestsBeforeRead, "history inspection must not call the model");
    assert.deepEqual(failures, []);
    assert(!(await page.locator("body").innerText()).includes("DSML"));
    console.log("Subagent E2E passed: ten simultaneous requests before any reply in multi-call and single-call batches, foreground/background, 40 distinct workers");
  } finally {
    if (app) await app.close().catch(() => {});
    server.closeAllConnections(); server.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
