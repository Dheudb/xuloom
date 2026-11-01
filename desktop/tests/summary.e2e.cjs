const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http");
const assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-summary-e2e-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), work = path.join(fixture, "work");
for (const dir of [data, config, work]) fs.mkdirSync(dir);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: work });
let app, page, sequence = 0, base;
const errors = [];
const server = http.createServer((req, res) => {
  if (req.method === "GET") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end('<html><head><title>Receipt-backed reference</title></head><body>Evidence. https://page-body.invalid/not-a-source</body></html>'); return;
  }
  let raw = ""; req.on("data", chunk => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
    const human = [...body.messages].reverse().find(m => m.role === "user" && typeof m.content === "string" && m.content.startsWith("SUMMARY_"));
    const after = human ? body.messages.slice(body.messages.indexOf(human) + 1).filter(m => m.role === "tool") : [];
    let tool;
    if (human?.content === "SUMMARY_FIRST") {
      if (after.length === 0) tool = { name: "file_write_tool", arguments: { file_path: "summary-output.md", content: "# Actual output\nSUMMARY_FILE_BODY\n" } };
      if (after.length === 1) tool = { name: "web_fetch_tool", arguments: { url: base + "/source" } };
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const send = (delta, finish_reason) => res.write("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (tool) { send({ tool_calls: [{ index: 0, id: "summary-" + ++sequence, type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.arguments) } }] }, null); send({}, "tool_calls"); }
    else { send({ content: human?.content === "SUMMARY_FIRST" ? "SUMMARY_DONE. Sources: https://assistant-only.invalid/not-evidence" : "SUMMARY_EMPTY_DONE" }, null); send({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_TEST_HEADLESS: "1",
    NO_PROXY: "127.0.0.1,localhost,::1", no_proxy: "127.0.0.1,localhost,::1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(40000); page.on("pageerror", e => errors.push(e.message));
  await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
  await app.evaluate(({ shell }) => { globalThis.sourceOpens = []; shell.openExternal = async url => { globalThis.sourceOpens.push(url); }; });
}
const menu = () => page.getByRole("dialog", { name: "输出与来源摘要", exact: true });
async function open() { await page.getByRole("button", { name: "输出与来源", exact: true }).click(); await menu().waitFor(); await page.waitForFunction(() => document.querySelector('.summary-scroll')?.getAttribute('aria-busy') === 'false'); }
async function send(text, answer) { await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); await page.getByText(answer, { exact: true }).waitFor(); await page.waitForFunction(() => !document.querySelector('.send-button.stop')); }
async function main() {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve)); base = `http://127.0.0.1:${server.address().port}`;
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "summary-fixture", openai_api_key: "fixture", openai_base_url: base + "/v1" });
    await launch(); await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    await open(); await menu().getByText("本轮尚无输出文件或联网来源。", { exact: true }).waitFor();
    await page.keyboard.press("Escape"); await menu().waitFor({ state: "detached" });
    await page.getByLabel("访问权限", { exact: true }).selectOption("full"); await page.waitForFunction(() => !document.querySelector('.permission-picker').disabled);
    await send("SUMMARY_FIRST", "SUMMARY_DONE. Sources: https://assistant-only.invalid/not-evidence");
    await open(); await menu().getByRole("button", { name: "summary-output.md", exact: true }).waitFor();
    const source = menu().getByRole("link", { name: /Receipt-backed reference/ }); await source.waitFor();
    assert.equal(await source.getAttribute("href"), base + "/source");
    assert.equal(await menu().getByRole("link").count(), 1); assert((await source.innerText()).includes("已读取网页"));
    await source.click(); assert.deepEqual(await app.evaluate(() => globalThis.sourceOpens), [base + "/source"]);
    await menu().getByRole("button", { name: "summary-output.md", exact: true }).click();
    await page.getByRole("region", { name: "文件预览" }).getByText("SUMMARY_FILE_BODY", { exact: false }).waitFor();
    await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
    await send("SUMMARY_SECOND", "SUMMARY_EMPTY_DONE");
    await open(); await menu().getByText("本轮尚无输出文件或联网来源。", { exact: true }).waitFor();
    await menu().getByRole("button", { name: "整个会话", exact: true }).click(); await menu().getByRole("link").waitFor();
    assert.equal(await menu().getByText(/部分旧记录/).count(), 0, "An ordinary empty turn is not a legacy tool record");
    await menu().getByRole("button", { name: "读取网页 1 次", exact: true }).click();
    await page.getByRole("textbox", { name: "搜索执行活动", exact: true }).waitFor();
    assert.equal(await page.getByRole("textbox", { name: "搜索执行活动", exact: true }).inputValue(), "web_fetch_tool");
    await open();
    await menu().getByRole("button", { name: /查看执行活动/ }).click(); await page.getByRole("heading", { name: /执行活动/ }).waitFor();
    await app.close(); app = null; await launch(); await page.locator('.session-item.active').waitFor();
    await open(); await menu().getByRole("button", { name: "整个会话", exact: true }).click(); await menu().getByRole("link").waitFor();
    await page.setViewportSize({ width: 920, height: 760 });
    const box = await menu().boundingBox(); assert(box.x >= 0 && box.x + box.width <= 920 && box.y + box.height <= 760, JSON.stringify(box));
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/session-summary.png"), Buffer.from(screenshot, "base64"));
    assert.deepEqual(errors, []);
    console.log("Summary E2E passed: receipt-only sources, real file preview, native link routing, empty turns, conversation scope, restart, keyboard dismissal, activity and narrow layout");
  } catch (error) { if (page) console.error(await page.locator("body").innerText().catch(() => "")); throw error; }
  finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
