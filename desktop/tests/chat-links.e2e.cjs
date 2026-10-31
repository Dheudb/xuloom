const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { atomicJSON, sessionKey } = require("../electron/catalog.cjs");
const { chatLink } = require("../electron/chat-links.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-links-ui-"));
const first = path.join(fixture, "first"), second = path.join(fixture, "second"), data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [first, second, data, config]) fs.mkdirSync(dir);
for (const [dir, id] of [[first, "a"], [second, "b"], [second, "c"]])
  atomicJSON(path.join(dir, ".xuloom/sessions/" + id + ".json"), { messages: [{ role: "user", content: "Saved " + id }, ...(id === "c" ? [{ role: "assistant", content: `[Open saved A](${chatLink(sessionKey(first, "a"))})` }] : [])], metadata: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [first, second], workspace: [{ project: first, resumeId: "a", active: true }] });
let app, page, env, clipboardBefore, requests = 0; const errors = [];
const server = http.createServer((_req, res) => { requests++; res.writeHead(500).end("Opening history must not call models"); });
const idA = sessionKey(first, "a"), idB = sessionKey(second, "b"), idC = sessionKey(second, "c");
const linkA = chatLink(idA), linkB = chatLink(idB), linkC = chatLink(idC);
async function launch(url) {
  env = { ...process.env, HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: [...(process.env.XULOOM_TEST_EXECUTABLE ? [] : [root]), ...(url ? [url] : [])], env });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", e => errors.push(e.message));
  await page.locator(".session-item.active").waitFor();
}
async function selected(id) {
  await page.waitForFunction(id => document.querySelector(".session-item.active")?.getAttribute("data-session-id") === id && !document.querySelector(".new-task").disabled, id);
}
const invoke = (method, params) => page.evaluate(({ method, params }) => window.xuloom.invoke(method, params), { method, params });
async function deliver(url) {
  await app.evaluate(({ app }, url) => app.emit("open-url", { preventDefault() {} }, url), url);
}
async function main() {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await selected(idA);
    clipboardBefore = await app.evaluate(({ clipboard }) => clipboard.readText());
    await page.locator(`.session-item[data-session-id="${idA}"]`).click({ button: "right" });
    await page.getByRole("menuitem", { name: "复制聊天链接", exact: true }).click();
    assert.equal(await app.evaluate(({ clipboard }) => clipboard.readText()), linkA);
    await selected(idA);
    await page.getByRole("textbox", { name: "任务输入" }).fill("Draft preserved across a link jump");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.getByRole("tab", { name: "工作区", exact: true }).click();
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => {
      const view = BrowserWindow.getAllWindows()[0].webContents;
      await view.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise(resolve => setTimeout(resolve, 100));
      return (await view.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/chat-links-settings.png"), Buffer.from(screenshot, "base64"));
    await page.getByRole("textbox", { name: "聊天链接", exact: true }).fill(linkB);
    await page.getByRole("button", { name: "打开会话", exact: true }).click(); await selected(idB);
    await deliver(linkA); await selected(idA);
    assert.equal(await page.getByRole("textbox", { name: "任务输入" }).inputValue(), "Draft preserved across a link jump");
    await deliver(linkA + "?prompt=NEVER_EXECUTE"); await page.getByText(/无效的 Xuloom 聊天链接/).waitFor(); await selected(idA);
    await invoke("session.update", { id: idB, patch: { archived: true } });
    await deliver(linkB); await page.getByText(/此会话或项目已归档/).waitFor(); await selected(idA);
    await invoke("archive.restore", { id: idB });
    await invoke("session.move", { id: idB, path: first });
    await deliver(linkB); await selected(sessionKey(first, "b"));
    await page.getByRole("article", { name: "你的消息", exact: true }).getByText("Saved b", { exact: true }).waitFor();
    assert(await page.getByRole("textbox", { name: "任务输入" }).isEnabled(), "A link must hydrate the conversation, not just select its sidebar label");
    assert.equal((await invoke("git.status", { session: sessionKey(first, "b") })).code, "not_repository");
    await invoke("session.delete", { id: sessionKey(first, "b"), confirm: true });
    await deliver(linkB); await page.getByText(/本机找不到此会话/).waitFor();
    await deliver(linkA); await selected(idA);
    // A real second executable invocation forwards its URL to the existing
    // isolated workbench. It must exit without opening another window.
    const child = spawn(process.env.XULOOM_TEST_EXECUTABLE || require("electron"), [...(process.env.XULOOM_TEST_EXECUTABLE ? [] : [root]), linkC], { env, windowsHide: true, stdio: "ignore" });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill(); reject(new Error("Owned URL launcher did not exit")); }, 30000);
      child.once("error", error => { clearTimeout(timer); reject(error); });
      child.once("exit", code => { clearTimeout(timer); code === 0 ? resolve() : reject(new Error("URL launcher exit " + code)); });
    });
    await selected(idC); assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
    await page.getByRole("link", { name: "Open saved A", exact: true }).click(); await selected(idA);
    await invoke("session.close", { session: idC });
    fs.renameSync(second, second + "-removed");
    await deliver(linkC); await page.getByText(/会话工作目录已不存在/).waitFor(); await selected(idA); assert(!fs.existsSync(second));
    fs.renameSync(second + "-removed", second);
    const registration = await invoke("chat.protocol", {}); assert.equal(typeof registration.registered, "boolean");
    await app.evaluate(({ clipboard }, value) => clipboard.writeText(value), clipboardBefore); clipboardBefore = undefined;
    await app.close(); await launch(linkC); await selected(idC);
    assert.equal(requests, 0); assert.deepEqual(errors, []);
    console.log("Chat links E2E passed: copy, paste, startup and real second instance; drafts, moves, archive/delete/missing workspace, no model calls");
  } finally {
    if (app) {
      if (clipboardBefore !== undefined) await app.evaluate(({ clipboard }, value) => clipboard.writeText(value), clipboardBefore).catch(() => {});
      await app.close().catch(() => {});
    }
    server.closeAllConnections(); server.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
