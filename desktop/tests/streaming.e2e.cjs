const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-streaming-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
let app, page, releaseFinish;
const calls = {}, failures = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", c => raw += c);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
    const taskMessage = [...body.messages].reverse().find(m => m.role === "user" && typeof m.content === "string" && m.content.includes("STREAM_"))?.content || "";
    const task = taskMessage.match(/STREAM_[A-Z]+/g)?.at(-1);
    calls[task] = (calls[task] || 0) + 1;
    if (["STREAM_HEADERS", "STREAM_STOP"].includes(task)) return; // Deliberately never send headers.
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (task === "STREAM_FINISH") {
      chunk({ content: "VISIBLE_BEFORE_FINISH" });
      releaseFinish = () => chunk({}, "stop"); // Leave socket open even after finish marker.
    } else if (task === "STREAM_REASONING") {
      let n = 0;
      chunk({ reasoning_content: "PRIVATE_REASONING" });
      const timer = setInterval(() => {
        if (++n < 5) chunk({ reasoning_content: "PRIVATE_REASONING" });
        else { clearInterval(timer); chunk({ content: "REASONING_DONE" }); chunk({}, "stop"); res.end("data: [DONE]\n\n"); }
      }, 180);
      res.on("close", () => clearInterval(timer));
    } else if (task === "STREAM_IDLE") {
      chunk({ content: "RETAIN_THIS_PARTIAL" });
      const timer = setInterval(() => res.write('data: {"id":"fixture","choices":[]}\n\n: keepalive\n\n'), 60);
      res.on("close", () => clearInterval(timer));
    } else if (task === "STREAM_DROPPED") {
      chunk({ content: "DROPPED_PARTIAL" }); res.end();
    } else { failures.push("Unexpected task " + task); res.end(); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: "", USERPROFILE: home, HOME: home, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(15000); page.on("pageerror", e => failures.push(e.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
}
async function idle() { await page.waitForFunction(() => !document.querySelector(".send-button.stop")); }
(async () => {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1`,
      stream_first_response_timeout_sec: 1.5, stream_idle_timeout_sec: 2, stream_end_grace_sec: .1 });
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await send("STREAM_FINISH");
    await page.getByText("VISIBLE_BEFORE_FINISH", { exact: true }).waitFor();
    assert(await page.getByRole("button", { name: "停止任务", exact: true }).isVisible(), "Text is visible while response is still streaming");
    releaseFinish(); await idle();
    assert.equal(await page.locator(".turn-process").last().getAttribute("data-turn-status"), "completed");
    await send("STREAM_REASONING"); await page.locator('.turn-process').filter({ hasText: "模型正在思考" }).waitFor();
    await page.getByText("REASONING_DONE", { exact: true }).waitFor(); await idle();
    assert(!(await page.locator("body").innerText()).includes("PRIVATE_REASONING"));
    await send("STREAM_IDLE"); await page.getByText("RETAIN_THIS_PARTIAL", { exact: true }).waitFor();
    await page.getByText(/连续 2 秒没有新的文本/).waitFor(); await idle();
    assert.equal(await page.locator(".turn-process").last().getAttribute("data-turn-status"), "error");
    assert.equal(calls.STREAM_IDLE, 1, "Stalled requests must not silently replay");
    await send("STREAM_DROPPED"); await page.getByText(/收到结束标记前断开/).waitFor(); await idle();
    await page.getByText("DROPPED_PARTIAL", { exact: true }).waitFor();
    await send("STREAM_HEADERS"); await page.locator('.turn-process').filter({ hasText: "等待接口响应" }).waitFor();
    await page.getByText(/首个有效响应超过 1.5 秒/).waitFor(); await idle();
    await send("STREAM_STOP"); await page.locator('.turn-process').filter({ hasText: "等待接口响应" }).last().waitFor();
    const stop = page.getByRole("button", { name: "停止任务", exact: true });
    assert(await stop.isEnabled()); await stop.click({ force: true }); await idle();
    assert.equal(await page.locator(".turn-process").last().getAttribute("data-turn-status"), "cancelled");
    await app.close(); app = null; await launch();
    await page.getByText("RETAIN_THIS_PARTIAL", { exact: true }).waitFor();
    await page.getByText("DROPPED_PARTIAL", { exact: true }).waitFor();
    assert((await page.locator('[title*="首个文本"]').count()) >= 1);
    const serialized = fs.readdirSync(path.join(data, "history")).flatMap(group => {
      const sessions = path.join(data, "history", group, "sessions");
      return fs.existsSync(sessions) ? fs.readdirSync(sessions).filter(f => f.endsWith(".json")).map(f => fs.readFileSync(path.join(sessions, f), "utf8")) : [];
    }).join("\n");
    assert(serialized.includes('"reasoningChunks": 5'));
    // Compatible thinking APIs require their protocol content on subsequent turns.
    // Keep it only in the private protocol field, never in visible content or traces.
    const records = serialized.split(/\n(?=\{)/).filter(Boolean).map(value => JSON.parse(value));
    assert(records.some(record => record.messages?.some(message => message._openai_reasoning?.content?.includes("PRIVATE_REASONING"))));
    const publicRecords = JSON.stringify(records, (key, value) => key === "_openai_reasoning" ? undefined : value);
    assert(!publicRecords.includes("PRIVATE_REASONING"));
    assert.deepEqual(failures, []);
    console.log("PASS: live text, finish marker without EOF, reasoning progress, keepalive stall, dropped stream, bounded header wait, stop and persisted timing/partial output.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
