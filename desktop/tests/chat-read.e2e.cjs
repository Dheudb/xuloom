const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const { atomicJSON } = require("../electron/catalog.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), crypto = require("node:crypto"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-chat-read-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home"), target = path.join(fixture, "other-project");
for (const directory of [data, config, home, target]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
const key = crypto.createHash("sha256").update(process.platform === "win32" ? target.toLowerCase() : target).digest("hex").slice(0, 16);
const folder = path.join(data, "history", key), saved = path.join(folder, "sessions", "evidence.json"), url = `xuloom://chat/v1/${key}/evidence`;
atomicJSON(path.join(folder, "project.json"), { version: 1, project: target });
atomicJSON(saved, { messages: [{ role: "system", content: "EXCLUDED_PRIVATE_SYSTEM" }, { role: "assistant", content: "CROSS_PROJECT_EVIDENCE" }], metadata: {} });
const original = fs.readFileSync(saved), modified = fs.statSync(saved).mtimeMs;
let app, page, calls = 0, serverError;
const errors = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", chunk => raw += chunk);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      calls++;
      let delta, finish;
      if (calls === 1) {
        assert(body.tools.some(tool => tool.function.name === "chat_read_tool"));
        delta = { tool_calls: [{ index: 0, id: "read-history", type: "function", function: { name: "chat_read_tool", arguments: JSON.stringify({ url, limit: 1 }) } }] };
        finish = "tool_calls";
      } else {
        assert.equal(calls, 2); assert(JSON.stringify(body.messages).includes("CROSS_PROJECT_EVIDENCE"));
        assert(!JSON.stringify(body.messages).includes("EXCLUDED_PRIVATE_SYSTEM"));
        delta = { content: "READ_HISTORY_OK" }; finish = "stop";
      }
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      res.write("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason: finish }], usage: { prompt_tokens: 10, completion_tokens: 5 } }) + "\n\n");
      res.end("data: [DONE]\n\n");
    } catch (error) { serverError = error; res.writeHead(500).end("fixture assertion failed"); }
  });
});
(async () => {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env: fixtureEnv({ data, config, home }), timeout: 30000 });
    page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", error => errors.push(error.message));
    await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
    await page.locator(".new-task").click();
    await page.getByRole("textbox", { name: "任务输入" }).fill("Read the saved evidence from " + url);
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByText("READ_HISTORY_OK", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    const trace = page.locator(".turn-process").first();
    if (!(await trace.evaluate(element => element.open))) await trace.locator(":scope > summary").click();
    await page.getByText("已读取会话", { exact: true }).waitFor();
    assert.equal(calls, 2); assert.equal(serverError, undefined); assert.deepEqual(errors, []);
    assert.deepEqual(fs.readFileSync(saved), original); assert.equal(fs.statSync(saved).mtimeMs, modified);
    await app.close(); app = null;
    const changed = fs.readdirSync(path.join(data, "history")).filter(group => group !== key).flatMap(group => {
      const directory = path.join(data, "history", group, "sessions");
      return fs.existsSync(directory) ? fs.readdirSync(directory).filter(name => name.endsWith(".json")).map(name => path.join(directory, name)) : [];
    });
    assert.equal(changed.length, 1); assert(fs.readFileSync(changed[0], "utf8").includes("READ_HISTORY_OK"));
    console.log("PASS: desktop model reads another project's history without navigation, private system fields, mutation or target execution; exactly two local model turns.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(serverError || error); process.exitCode = 1; });
