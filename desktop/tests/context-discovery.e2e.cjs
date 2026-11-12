const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-context-discovery-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
let app, requests = 0;
const failures = [];
const server = http.createServer((req, res) => {
  try {
    assert.equal(req.method, "GET"); assert.equal(req.url, "/v1/models");
    assert.equal(req.headers.authorization, "Bearer fixture-key"); requests++;
    res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ data: [
      { id: "one", max_model_len: 1048576 }, { id: "two", context_window: 262144 },
    ] }));
  } catch (error) { failures.push(error.stack); res.writeHead(500).end("fixture error"); }
});
(async () => {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const cfg = path.join(config, "config.json");
    atomicJSON(cfg, { model_provider: "openai", active_model_connection: "fixture", model_connections: [{
      id: "fixture", name: "Fixture", provider: "openai", base_url: `http://127.0.0.1:${server.address().port}/v1`,
      api_key: "fixture-key", model: "one", models: ["one", "two"], model_context: { one: { window: 131072, reserve: 0, keepRecent: 0, backend: "summary" } },
    }] });
    const before = fs.readFileSync(cfg, "utf8");
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE,
      args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env: fixtureEnv({ data, config, home }), timeout: 30000 });
    const page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", error => failures.push(error.message));
    await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.getByRole("button", { name: "管理模型", exact: true }).click();
    await page.getByRole("button", { name: "从 API 获取模型", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "识别 2 个上下文窗口" }).waitFor();
    assert.equal(fs.readFileSync(cfg, "utf8"), before, "Discovery does not silently save configuration");
    await page.locator(".model-options > summary").click();
    assert.equal(await page.getByLabel("模型上下文窗口", { exact: true }).inputValue(), "131072", "Manual limit survives discovery");
    await page.locator(".configured-models").getByRole("button", { name: "two", exact: true }).click();
    assert.equal(await page.getByLabel("模型上下文窗口", { exact: true }).inputValue(), "262144");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "工作台设置" }).waitFor({ state: "hidden" });
    const saved = JSON.parse(fs.readFileSync(cfg, "utf8")).model_connections[0];
    assert.equal(saved.model_context.one.window, 131072); assert.equal(saved.model_context.two.window, 262144);
    assert.equal(requests, 1, "No model generation or extra capacity probes"); assert.deepEqual(failures, []);
    console.log("PASS: metadata windows appear in settings, manual overrides survive, Save persists both; one GET, zero generation requests.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
