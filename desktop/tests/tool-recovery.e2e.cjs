const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-tool-recovery-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
let app, page, sequence = 0, mainCalls = 0, exhaustedCalls = 0, childCalls = 0, workflowCalls = 0;
let releaseRepair, repairReady;
const repairPending = new Promise(resolve => repairReady = resolve);
const python = process.env.XULOOM_PYTHON || path.join(root, "build", "python", "python.exe");
const failures = [];
const bad = '<｜DSML｜tool_calls><｜DSML｜invoke name="command_tool"><｜DSML｜parameter name="unknown" string="true">bad</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>';
function reply(res, text, name, args, raw = false) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
  if (name) {
    chunk({ tool_calls: [{ index: 0, id: "fixture_" + ++sequence, type: "function", function: { name, arguments: raw ? args : JSON.stringify(args) } }] });
    chunk({}, "tool_calls");
  } else { chunk({ content: text }); chunk({}, "stop"); }
  res.end("data: [DONE]\n\n");
}
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      const text = JSON.stringify(body.messages);
      const child = body.messages.some(m => m.role === "user" && typeof m.content === "string" && m.content.startsWith("CHILD_TEST"));
      if (child) {
        childCalls++;
        if (childCalls === 1) return reply(res, bad);
        if (childCalls === 2) { assert(text.includes("Unknown parameter unknown")); return reply(res, null, "command_tool", { action: "capabilities" }); }
        return reply(res, "CHILD_PASS");
      }
      const task = [...body.messages].reverse().find((m) => m.role === "user" && typeof m.content === "string" && /^(RECOVER|EXHAUST|CONTINUE|CHILD_RECOVER|WORKFLOW)(?:$|\n|\[)/.test(m.content));
      const taskName = task?.content.match(/^\w+/)?.[0];
      if (taskName === "RECOVER") {
        switch (++mainCalls) {
          case 1: return reply(res, null, "file_write_tool", { file_path: "before.txt", content: "SAVED_BEFORE" });
          case 2: return reply(res, bad);
          case 3:
            assert(text.includes("Unknown parameter unknown")); assert(text.includes("before.txt"));
            releaseRepair = () => reply(res, null, "command_tool", '{"action":', true); repairReady(); return;
          case 4:
            assert(text.includes("This call was not executed"));
            return reply(res, '<｜DSML｜tool_calls><｜DSML｜invoke name="command_tool"><｜DSML｜parameter name="arguments" string="false">{"action":"capabilities","wait_ms":30000}</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>');
          case 5:
            assert.equal(JSON.parse([...body.messages].reverse().find((m) => m.role === "tool").content).max_wait_ms, 120000);
            return reply(res, null, "file_write_tool", { file_path: "after.txt", content: "SAVED_AFTER" });
          case 6: return reply(res, "RECOVERY_PASS");
          default: throw new Error("Unbounded main repair");
        }
      }
      if (taskName === "EXHAUST") { assert(++exhaustedCalls <= 3); return reply(res, bad); }
      if (taskName === "CONTINUE") return reply(res, "CONTINUATION_PASS");
      if (taskName === "WORKFLOW") {
        switch (++workflowCalls) {
          case 1: return reply(res, null, "command_tool", { argv: [python, "-c", "print('{\"run_handle\":\"fixture:one\",\"workflow_finished\":false}')"], wait_ms: 1000, max_bytes: 4 });
          case 2: return reply(res, "I will read the cards next.");
          case 3:
            assert(text.includes("Skill workflow completion guard"));
            return reply(res, null, "command_tool", { argv: [python, "-c", "print('{\"run_handle\":\"fixture:one\",\"workflow_finished\":true}')"], wait_ms: 1000 });
          case 4: return reply(res, "WORKFLOW_PASS");
          default: throw new Error("Unbounded workflow continuation");
        }
      }
      if (taskName === "CHILD_RECOVER") {
        if (!body.tools.some(t => t.function.name === "agent_tool")) return reply(res, null, "tool_search_tool", { query: "agent_tool", activate: ["agent_tool"] });
        const result = body.messages.slice(body.messages.indexOf(task) + 1).find((m) => m.role === "tool" && m.content.includes("CHILD_PASS"));
        if (!result) return reply(res, null, "agent_tool", { prompt: "CHILD_TEST", response_format: "json", run_in_background: false });
        assert(result.content.includes("CHILD_PASS")); return reply(res, "CHILD_PARENT_PASS");
      }
      throw new Error("Unexpected fixture request");
    } catch (error) { failures.push(error.stack); res.writeHead(500).end("fixture failed"); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, USERPROFILE: home, HOME: home, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", (error) => failures.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
(async () => {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    const skill = path.join(home, ".xuloom", "skills", "fixture-workflow"); fs.mkdirSync(skill, { recursive: true });
    fs.writeFileSync(path.join(skill, "SKILL.md"), "---\nname: fixture-workflow\ndescription: Fixture workflow.\nmetadata:\n  xuloom:\n    completion-protocol: workflow-json-v1\n---\nFollow the fixture runtime.");
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full"); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    const recovering = send("RECOVER");
    await Promise.race([repairPending, new Promise((_, reject) => setTimeout(() => reject(new Error("Repair request did not arrive")), 60000).unref())]);
    await page.locator('.turn-process > summary').filter({ hasText: "模型正在重试" }).waitFor();
    const liveRetry = page.locator('.turn-retry[data-retry-status="retrying"]').first();
    assert((await liveRetry.locator('summary').innerText()).includes("Retry 1/2"));
    assert(!(await liveRetry.locator('.turn-retry-detail').isVisible()));
    await page.screenshot({ path: path.join(root, 'build', 'retry-status-live.png') });
    assert(!(await page.locator('.turn-commentary').allTextContents()).join('').includes("正在请求模型修正"));
    releaseRepair(); await recovering;
    await page.getByText("RECOVERY_PASS", { exact: true }).waitFor(); assert.equal(mainCalls, 6);
    let trace = page.locator(".turn-process").first(); assert.equal(await trace.getAttribute("data-turn-status"), "completed");
    await trace.locator(":scope > summary").click(); assert.equal(await trace.locator(".turn-retry").count(), 2); assert.equal(await trace.locator(".turn-tool").count(), 4);
    assert(!(await trace.locator(':scope > summary').innerText()).includes("重试"));
    await trace.locator('.turn-retry > summary').first().click(); assert((await trace.locator('.turn-retry-detail').first().innerText()).includes("正在请求模型修正"));
    const workspace = JSON.parse(fs.readFileSync(path.join(data, "workbench.json"), "utf8")).activeConversation.project;
    assert.equal(fs.readFileSync(path.join(workspace, "before.txt"), "utf8"), "SAVED_BEFORE"); assert.equal(fs.readFileSync(path.join(workspace, "after.txt"), "utf8"), "SAVED_AFTER");
    await send("EXHAUST"); await page.locator('.session-error').filter({ hasText: "工具调用格式多次修正失败" }).waitFor(); assert.equal(exhaustedCalls, 3);
    assert.equal(await page.locator('.turn-process[data-turn-status="error"]').count(), 1);
    await send("CONTINUE"); await page.getByText("CONTINUATION_PASS", { exact: true }).waitFor();
    await send("CHILD_RECOVER"); await page.getByText("CHILD_PARENT_PASS", { exact: true }).waitFor(); assert.equal(childCalls, 3);
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "Skills", exact: true }).click();
    await page.locator(".capability-card").filter({ has: page.getByText("fixture-workflow", { exact: true }) }).getByRole("button", { name: "选择使用", exact: true }).click();
    await send("WORKFLOW"); await page.getByText("WORKFLOW_PASS", { exact: true }).waitFor(); assert.equal(workflowCalls, 4);
    const workflowTrace = page.locator(".turn-process").last(); assert.equal(await workflowTrace.getAttribute("data-turn-status"), "completed");
    await workflowTrace.locator(":scope > summary").click(); await workflowTrace.locator('.turn-retry > summary').click(); assert((await workflowTrace.innerText()).includes("技能流程尚未完成"));
    await app.close(); app = null; await launch(); await page.getByText("RECOVERY_PASS", { exact: true }).waitFor();
    trace = page.locator(".turn-process").first(); await trace.locator(":scope > summary").click();
    assert.equal(await trace.locator(".turn-retry").count(), 2); await trace.locator('.turn-retry > summary').first().click(); assert((await trace.innerText()).includes("正在请求模型修正"));
    assert.equal(await page.locator('.turn-process[data-turn-status="error"]').count(), 1); assert.deepEqual(failures, []);
    await page.getByText("WORKFLOW_PASS", { exact: true }).waitFor();
    console.log("PASS: DSML/native repair, prior files retained, bounded failure, continuation, child recovery, unfinished skill continuation and restart trace.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
