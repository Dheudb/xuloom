// Real hidden Electron -> Python bridge -> local SSE -> durable worker records.
const { _electron: electron } = require("playwright");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-pipeline-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [data, config]) fs.mkdirSync(dir, { recursive: true });
let app, page;
const calls = [], errors = [];
const server = http.createServer((req, res) => {
  let raw = "";
  req.on("data", part => raw += part);
  req.on("end", () => {
    try {
      assert.equal(req.headers.authorization, "Bearer OWNED_PIPELINE_FIXTURE");
      const body = JSON.parse(raw);
      const task = body.messages.find(message => message.role === "user")?.content;
      calls.push(JSON.stringify(task));
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.end('data: ' + JSON.stringify({ choices: [{ index: 0, delta: { role: "assistant", content: "Verified local stage output " + calls.length }, finish_reason: null }] }) + '\n\ndata: ' + JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 20, completion_tokens: 10 } }) + '\n\ndata: [DONE]\n\n');
    } catch (error) { errors.push(error.message); res.writeHead(500).end(); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(30000);
  page.on("pageerror", error => errors.push(error.message));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
  await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
}
async function openTree() {
  const expand = page.getByRole("button", { name: "展开能力面板", exact: true });
  if (await expand.count()) await expand.click();
  await page.locator(".panel-tabs").getByRole("button", { name: "代理", exact: true }).click();
  await page.getByLabel("协作运行树").waitFor();
}
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  atomicJSON(path.join(config, "config.json"), { model_provider: "openai", active_model_connection: "owned", model_connections: [{
    id: "owned", name: "Owned fixture", provider: "openai", model: "pipeline-fixture", models: ["pipeline-fixture"],
    api_key: "OWNED_PIPELINE_FIXTURE", base_url: `http://127.0.0.1:${server.address().port}/v1`,
  }] });
  try {
    await launch();
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker")?.disabled);
    await openTree();
    const spec = { items: [{ id: "paper-a", input: "Source A" }, { id: "paper-b", input: "Source B" }],
      stages: [{ id: "read", prompt: "Read the source" }, { id: "summarize", prompt: "Summarize the previous result" }] };
    const file = path.join(fixture, "pipeline.json"); atomicJSON(file, spec);
    await page.locator(".pipeline-tree input[type=file]").setInputFiles(file);
    await page.waitForFunction(() => document.querySelector(".pipeline-run > summary small")?.textContent === "已完成", null, { timeout: 60000 });
    assert.equal(calls.length, 4);
    assert(calls.some(task => task.includes("Previous stage result")));
    assert.equal(await page.locator(".pipeline-run .pipeline-stage").count(), 4);
    await page.locator(".pipeline-run > details").first().locator("summary").click();
    await page.locator(".pipeline-stage").first().getByRole("button", { name: "read", exact: true }).click();
    await page.getByRole("dialog", { name: "子代理完整历史" }).getByText("Verified local stage output", { exact: false }).first().waitFor();
    await page.getByRole("button", { name: "关闭子代理历史", exact: true }).click();
    const sid = await page.locator(".session-item.active").getAttribute("data-session-id");
    await app.close(); app = null;
    await launch(); await openTree();
    await page.waitForFunction(() => document.querySelector(".pipeline-run > summary small")?.textContent === "已完成");
    const tree = await page.evaluate(async session => window.xuloom.invoke("agents.tree", { session }), sid);
    assert.equal(tree.runs.length, 1); assert.equal(tree.runs[0].status, "completed");
    assert.equal(calls.length, 4, "Inspection/restart must not issue new model requests");
    const png = await app.evaluate(async ({ BrowserWindow }) => {
      const contents = BrowserWindow.getAllWindows()[0].webContents;
      await contents.capturePage(undefined, { stayHidden: true, stayAwake: true });
      return (await contents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.writeFileSync(path.join(root, "test-results/pipeline-tree.png"), Buffer.from(png, "base64"));
    assert.deepEqual(errors, []);
    console.log("Pipeline E2E passed: JSON import, four real SSE worker requests, bound downstream inputs, run tree, history, restart without replay; owned localhost only");
  } finally {
    if (app) await app.close().catch(() => {});
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
