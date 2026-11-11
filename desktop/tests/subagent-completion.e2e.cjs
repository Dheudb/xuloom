const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-agent-completion-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
let app, page, mainRequests = 0, arrived;
const childResponses = [], failures = [], allArrived = new Promise((resolve) => arrived = resolve);
function reply(res, text, tool) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
  if (tool) {
    chunk({ tool_calls: [{ index: 0, id: "spawn_batch", type: "function", function: { name: "agent_tool", arguments: JSON.stringify(tool) } }] }); chunk({}, "tool_calls");
  } else { chunk({ content: text }); chunk({}, "stop"); }
  res.end("data: [DONE]\n\n");
}
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      if (!body.tools.some((t) => ["agent_tool", "spawn_agent"].includes(t.function.name))) {
        childResponses.push(res); if (childResponses.length === 3) arrived(); return;
      }
      mainRequests++;
      if (mainRequests === 1) return reply(res, null, { prompt: "Return your assigned output", count: 3, run_in_background: true, response_format: "json" });
      if (mainRequests === 2) return reply(res, "PREMATURE_FINAL");
      assert.equal(mainRequests, 3, "Waiting must not loop through model requests");
      const history = JSON.stringify(body.messages);
      for (const expected of ["OUTPUT_FIRST", "OUTPUT_SECOND", "crashed", "empty_result", "Delegation completion guard"]) assert(history.includes(expected), expected);
      return reply(res, "SUMMARY_WITH_2_RESULTS_AND_1_FAILURE");
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
(async () => {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full"); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await page.getByRole("textbox", { name: "任务输入" }).fill("Launch and summarize three workers"); await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await Promise.race([allArrived, new Promise((_, reject) => setTimeout(() => reject(new Error("Workers did not all arrive")), 30000))]);
    await page.getByText("等待 3 个子代理返回结果，全部结束后再汇总。", { exact: true }).waitFor();
    assert.equal(mainRequests, 2); assert.equal(await page.locator(".send-button.stop").count(), 1);
    assert.equal(await page.locator('.turn-process[data-turn-status="completed"]').count(), 0);
    reply(childResponses[0], "OUTPUT_FIRST");
    await page.getByText("等待 2 个子代理返回结果，全部结束后再汇总。", { exact: true }).waitFor();
    assert.equal(mainRequests, 2); assert.equal(await page.locator(".send-button.stop").count(), 1);
    reply(childResponses[1], "OUTPUT_SECOND");
    await page.getByText("等待 1 个子代理返回结果，全部结束后再汇总。", { exact: true }).waitFor();
    assert.equal(mainRequests, 2);
    reply(childResponses[2], "   ");
    await page.getByText("SUMMARY_WITH_2_RESULTS_AND_1_FAILURE", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(mainRequests, 3);
    await app.close(); app = null; await launch(); await page.getByText("SUMMARY_WITH_2_RESULTS_AND_1_FAILURE", { exact: true }).waitFor();
    const trace = page.locator(".turn-process").first(); await trace.locator(":scope > summary").click();
    assert((await trace.innerText()).includes("等待 3 个子代理")); assert.equal(await page.locator(".message.assistant").count(), 1);
    assert.deepEqual(failures, []);
    console.log("PASS: premature final held until every worker settles, failed empty output included, no model polling, restart trace retained.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
