const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const { atomicJSON } = require("../electron/catalog.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-output-budget-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const directory of [data, config, home]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
let app, page, calls = 0;
const failures = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", chunk => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
    calls++;
    assert(Number.isFinite(body.max_tokens) && body.max_tokens > 0 && body.max_tokens <= 64000);
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write("data: " + JSON.stringify({ choices: [{ index: 0, delta: { content: calls === 1 ? "FIRST_OK" : "CONTINUED_OK" }, finish_reason: "stop" }], usage: { prompt_tokens: 10, completion_tokens: 5 } }) + "\n\n");
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE,
    args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env: fixtureEnv({ data, config, home }), timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", error => failures.push(error.message));
  await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
}
async function send(text, answer) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.getByText(answer, { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
(async () => {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture",
      openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click(); await send("FIRST", "FIRST_OK");
    await app.close(); app = null;
    const history = path.join(data, "history");
    const files = fs.readdirSync(history).flatMap(group => {
      const directory = path.join(history, group, "sessions");
      return fs.existsSync(directory) ? fs.readdirSync(directory).filter(name => name.endsWith(".json")).map(name => path.join(directory, name)) : [];
    });
    assert.equal(files.length, 1);
    const saved = JSON.parse(fs.readFileSync(files[0], "utf8"));
    saved.metadata.session_token_usage.output_tokens = 204217;
    saved.metadata.last_run_result = { reason: "budget_exhausted", persisted: true };
    saved.metadata.turn_traces.at(-1).status = "error";
    atomicJSON(files[0], saved);
    await launch(); await page.getByText("FIRST_OK", { exact: true }).waitFor();
    await send("CONTINUE_AFTER_BUDGET", "CONTINUED_OK");
    await app.close(); app = null;
    const continued = JSON.parse(fs.readFileSync(files[0], "utf8"));
    assert.equal(continued.metadata.session_token_usage.output_tokens, 204222);
    assert.equal(continued.metadata.last_run_result.reason, "completed");
    assert.equal(calls, 2); assert.deepEqual(failures, []);
    console.log("PASS: reopened conversation with 204217 output tokens continues, retains history/accounting and finite per-response limits.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
