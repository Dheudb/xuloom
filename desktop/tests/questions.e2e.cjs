const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-questions-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [] });
const failures = [], counts = {}, requests = [];
let app, page, sequence = 0;
const choices = [{ label: "休息", description: "在家放松" }, { label: "旅行", description: "到户外看看" }];
function reply(res, content, input) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const delta = input ? { tool_calls: [{ index: 0, id: "question-" + ++sequence, type: "function",
    function: { name: "ask_user_question_tool", arguments: JSON.stringify(input) } }] } : { content };
  res.end("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason: input ? "tool_calls" : "stop" }] }) + "\n\ndata: [DONE]\n\n");
}
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", chunk => raw += chunk); req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      requests.push(body);
      const task = [...body.messages].reverse().find(m => m.role === "user" && typeof m.content === "string" && /^QUESTION_(REPAIR|MULTI|CANCEL)(?:$|\n)/.test(m.content));
      const name = task.content.split("\n")[0], count = counts[name] = (counts[name] || 0) + 1;
      const text = JSON.stringify(body.messages);
      if (name === "QUESTION_REPAIR") {
        if (count === 1) {
          // The question tool is deferred behind tool_search_tool (lean core tool set) but still runs when called;
          // its compact schema is covered by tests/test_questions.py, so only check it here when it was sent.
          const schema = (body.tools || []).find(t => t.function.name === "ask_user_question_tool")?.function.parameters;
          if (schema) {
            assert(schema.properties.questions.items.properties.options.items.properties.label);
            assert(!JSON.stringify(schema).includes('"$ref"'));
          }
          return reply(res, null, { questions: choices });
        }
        if (count === 2) {
          assert(text.includes("Valid example")); assert(text.includes("executed: False"));
          return reply(res, null, { questions: [{ question: "理想周末？", options: choices }] });
        }
        assert.equal(count, 3); assert(text.includes("A: 旅行")); return reply(res, "QUESTION_REPAIR_PASS");
      }
      if (name === "QUESTION_MULTI") {
        if (count === 1) return reply(res, null, { questions: [{ question: "可多选", options: choices, multiSelect: true },
          { question: "补充偏好", options: choices }] });
        assert.equal(count, 2); assert(text.includes("A: 休息, 旅行")); assert(text.includes("A: 自定义回答"));
        return reply(res, "QUESTION_MULTI_PASS");
      }
      assert.equal(count, 1); return reply(res, null, { questions: [{ question: "等待取消", options: choices }] });
    } catch (error) { failures.push(error.stack); res.writeHead(500).end("fixture failed"); }
  });
});
async function send(text) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
}
(async () => {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    const env = fixtureEnv({ data, config, home: home });
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE,
      args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
    page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", e => failures.push(e.message));
    await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
    await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("QUESTION_REPAIR");
    const card = page.getByRole("region", { name: "待处理请求" });
    await card.getByText("理想周末？", { exact: true }).waitFor();
    assert.equal(counts.QUESTION_REPAIR, 2, "Must wait for actual user input even in Full Access");
    assert(await card.getByRole("button", { name: "提交回答" }).isDisabled());
    await card.getByRole("button", { name: "旅行", exact: false }).click();
    await card.getByRole("button", { name: "提交回答" }).click();
    await page.getByText("QUESTION_REPAIR_PASS", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    await send("QUESTION_MULTI"); await card.getByText("可多选", { exact: true }).waitFor();
    const multi = card.locator(".question").filter({ has: page.getByText("可多选", { exact: true }) });
    await multi.getByRole("button", { name: "休息", exact: false }).click();
    await multi.getByRole("button", { name: "旅行", exact: false }).click();
    assert.equal(await multi.locator(".chosen").count(), 2);
    await card.locator(".question").filter({ has: page.getByText("补充偏好", { exact: true }) }).getByPlaceholder("或者输入你的回答").fill("自定义回答");
    await card.getByRole("button", { name: "提交回答" }).click();
    await page.getByText("QUESTION_MULTI_PASS", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    await send("QUESTION_CANCEL"); await card.getByText("等待取消", { exact: true }).waitFor();
    await page.locator(".send-button.stop").click();
    await page.waitForFunction(() => !document.querySelector(".interaction") && !document.querySelector(".send-button.stop"));
    assert.equal(counts.QUESTION_CANCEL, 1); assert.deepEqual(failures, []);
    console.log("PASS: invalid question repair, real single/multiple/custom answers, Full Access waits, cancellation clears picker.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
