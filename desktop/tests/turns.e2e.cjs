const { _electron: electron } = require("playwright");
const { fixtureEnv } = require("./e2e-env.cjs");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-turns-e2e-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "profile"), config = path.join(fixture, "config");
for (const directory of [project, data, config]) fs.mkdirSync(directory);
fs.writeFileSync(path.join(project, "proof.txt"), "TURN_OUTPUT_PROOF");
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe") : path.join(root, "../.venv-mcp/Scripts/python.exe");
const picture = path.join(project, "landscape.png");
require("node:child_process").execFileSync(python, ["-I", "-c", "from PIL import Image,ImageDraw; import sys; im=Image.new('RGB',(720,420),'#e9f3fa'); d=ImageDraw.Draw(im); d.ellipse((480,40,600,160),fill='#f4cf76'); d.polygon([(0,350),(180,170),(400,420)],fill='#89b5c8'); d.polygon([(250,420),(530,210),(720,420)],fill='#bed1be'); im.save(sys.argv[1])", picture], { windowsHide: true });
let mediaRequests = 0, modelReceivedImage = false;
let releaseFirstTurn;
const firstTurnGate = new Promise(resolve => { releaseFirstTurn = resolve; });
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: project });
let app, page;
const errors = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", async () => {
    const body = JSON.parse(raw);
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
    const task = [...body.messages].reverse().find((m) => m.role === "user" && typeof m.content === "string" && m.content.startsWith("TRACE_"));
    const after = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
    const calls = after.filter((m) => m.role === "tool").length;
    if (task?.content === "TRACE_MEDIA") {
      mediaRequests++;
      modelReceivedImage ||= JSON.stringify(after).includes('"image_url"');
    }
    if (task?.content === "TRACE_ERROR" && calls) { res.writeHead(401, { "Content-Type": "application/json" }).end(JSON.stringify({ error: { message: "fixture error", type: "authentication_error" } })); return; }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    const multi = task?.content === "TRACE_MULTI" && calls < 2;
    const cancel = task?.content === "TRACE_CANCEL" && !calls;
    const error = task?.content === "TRACE_ERROR" && !calls;
    if (task?.content === "TRACE_MEDIA" && !calls) {
      chunk({ role: "assistant", content: "我会查看图片，整理资料，并保存说明。" });
      const tools = [
        ["file_read_tool", { file_path: picture }],
        ["grep_tool", { pattern: "TURN_OUTPUT_PROOF", path: path.join(project, "proof.txt") }],
        ["command_tool", { command: "echo TOOL_TERMINAL_PROOF", wait_ms: 1000 }],
        ["file_write_tool", { file_path: path.join(project, "image-notes.md"), content: "# 图片说明\n蓝色山丘与阳光。\n" }],
      ];
      chunk({ tool_calls: tools.map(([name, input], index) => ({ index, id: "media_" + index, type: "function", function: { name, arguments: JSON.stringify(input) } })) });
      chunk({}, "tool_calls");
    } else if (multi || cancel || error) {
      chunk({ role: "assistant", content: cancel ? "先请求批准，再写文件。" : calls ? "再检查一次，确认结果。" : "我先检查文件内容。" });
      // Keep a real request open after the first tool has finished, until the UI verifies its running state.
      if (multi && calls === 1) await firstTurnGate;
      chunk({ tool_calls: [{ index: 0, id: `call_${calls}`, type: "function", function: { name: cancel ? "file_write_tool" : "file_read_tool", arguments: JSON.stringify({ file_path: path.join(project, cancel ? "cancelled.txt" : "proof.txt"), ...(cancel ? { content: "never write" } : {}) }) } }] });
      chunk({}, "tool_calls");
    } else {
      chunk({ role: "assistant", content: task?.content === "TRACE_SECOND" ? "第二轮的独立回答。" : "文件检查完成，这是最终答案。" });
      chunk({}, "stop");
    }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = fixtureEnv({ data, config, home: path.join(fixture, "home") });
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(60000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) { await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function finished() { await page.waitForFunction(() => !document.querySelector(".send-button.stop")); }
async function main() {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "turn-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    await send("TRACE_MULTI");
    await page.locator('.turn-process[data-turn-status="running"][open]').waitFor();
    await page.waitForFunction(() => /用时 [1-9]\d*秒/.test(document.querySelector('.turn-process[data-turn-status="running"] > summary')?.textContent || ""));
    releaseFirstTurn();
    await finished();
    const first = page.locator(".turn-process").first();
    assert.equal(await first.getAttribute("open"), null);
    assert.match(await first.locator(":scope > summary").innerText(), /用时 [1-9]\d*秒/);
    await first.locator(":scope > summary").click();
    assert.equal(await first.locator(".turn-commentary").count(), 2);
    assert((await first.innerText()).includes("我先检查文件内容。"));
    assert((await first.innerText()).includes("再检查一次，确认结果。"));
    assert.equal(await first.locator(".turn-tool").count(), 2);
    await first.locator(".turn-tool > summary").first().click();
    assert((await first.innerText()).includes("TURN_OUTPUT_PROOF"));
    assert(!(await first.innerText()).includes("这是最终答案"));
    await send("TRACE_SECOND"); await finished();
    assert.equal(await page.locator(".turn-process").count(), 2);
    assert.equal(await page.locator(".turn-process").nth(1).locator(".turn-tool").count(), 0);
    assert.equal(await page.locator(".turn-process").nth(1).evaluate((element) => element.tagName), "DIV");
    assert.equal(await page.locator(".turn-process").nth(1).locator("summary,svg,.turn-process-content").count(), 0);
    assert.match(await page.locator(".turn-process").nth(1).innerText(), /用时/);
    await send("TRACE_CANCEL"); await page.getByRole("button", { name: "允许一次", exact: true }).waitFor();
    await page.getByRole("button", { name: "停止任务", exact: true }).click(); await finished();
    assert.equal(await page.locator(".turn-process").nth(2).getAttribute("data-turn-status"), "cancelled");
    assert.equal(await page.locator(".turn-process").nth(2).getAttribute("open"), null);
    assert(!fs.existsSync(path.join(project, "cancelled.txt")));
    await send("TRACE_ERROR"); await finished();
    assert.equal(await page.locator(".turn-process").nth(3).getAttribute("data-turn-status"), "error");
    const summary = await first.locator(":scope > summary").innerText();
    await app.close(); await launch();
    assert.equal(await page.locator(".turn-process").count(), 4);
    assert.equal(await page.locator(".turn-process[open]").count(), 0);
    assert.equal(await page.locator(".turn-process-time").count(), 1);
    assert.equal(await page.locator(".turn-process").first().locator(":scope > summary").innerText(), summary);
    await page.locator(".turn-process").first().locator(":scope > summary").click();
    assert.equal(await page.locator(".turn-process").first().locator(".turn-commentary").count(), 2);
    assert.equal(await page.locator(".message.assistant").count(), 4);
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("TRACE_MEDIA"); await finished();
    const mediaTurn = () => page.locator(".turn-process").last();
    await mediaTurn().locator(":scope > summary").click();
    for (const kind of ["image", "search", "terminal", "edit"]) assert.equal(await mediaTurn().locator(`[data-tool-kind="${kind}"]`).count(), 1);
    assert.equal(await mediaTurn().locator(".turn-tool.completed").count(), 4);
    const thumbnail = () => mediaTurn().getByRole("button", { name: "放大图像 landscape.png" });
    await thumbnail().scrollIntoViewIfNeeded();
    await thumbnail().locator("img").waitFor();
    await thumbnail().click();
    await page.getByRole("dialog", { name: "图片附件预览" }).waitFor();
    assert.equal(await page.getByRole("dialog").locator("img").evaluate(im => im.naturalWidth), 720);
    await page.getByRole("button", { name: "关闭图片预览" }).click();
    assert(modelReceivedImage, "the native image must reach the model, not only the UI");
    assert.equal(mediaRequests, 2, "preview must not call the model");
    fs.unlinkSync(picture);
    await app.close(); await launch();
    await mediaTurn().locator(":scope > summary").click();
    await thumbnail().scrollIntoViewIfNeeded(); await thumbnail().locator("img").waitFor();
    await thumbnail().click(); await page.getByRole("dialog", { name: "图片附件预览" }).waitFor();
    await page.getByRole("button", { name: "关闭图片预览" }).click();
    assert.equal(mediaRequests, 2, "history preview must not rerun image tools or model requests");
    await mediaTurn().scrollIntoViewIfNeeded();
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    // Playwright's screenshot waits for a visible compositor in hidden windows.
    // Electron can wake the renderer and capture without showing the test app.
    const shot = await app.evaluate(async ({ BrowserWindow }) => {
      const web = BrowserWindow.getAllWindows()[0].webContents;
      await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise((resolve) => setTimeout(resolve, 100));
      return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.writeFileSync(path.join(root, "test-results/turn-process.png"), Buffer.from(shot, "base64"));
    assert.deepEqual(errors, []);
    console.log("Turn process E2E passed: typed compact rows, native model image input, thumbnail/enlargement after original deletion and restart, live timer, collapse, details, cancellation/error and per-turn isolation");
  } finally { releaseFirstTurn(); if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
