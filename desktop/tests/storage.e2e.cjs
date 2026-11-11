const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs"), { historyRoot } = require("../electron/history-storage.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-storage-e2e-"));
const shared = path.join(fixture, "legacy-shared"), data = path.join(fixture, "profile"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const directory of [shared, data, config, home]) fs.mkdirSync(directory);
fs.writeFileSync(path.join(shared, "artifact.txt"), "existing output");
for (const id of ["one", "two"]) atomicJSON(path.join(shared, ".xuloom/sessions", id + ".json"), {
  messages: [{ role: "user", content: "旧独立会话 " + id }, { role: "assistant", content: "已写入：`artifact.txt`。" }],
  metadata: { file_changes: [{ path: path.join(shared, "artifact.txt"), operation: "create", added: 1, removed: 0, statsAvailable: true, updatedAt: 1, diff: "@@ -0,0 +1 @@\n+existing output" }] },
});
atomicJSON(path.join(shared, ".xuloom/session-index.json"), { version: 1, sessions: { one: { scope: "standalone", title: "旧独立会话 one", pinned: true }, two: { scope: "standalone", title: "旧独立会话 two" } } });
atomicJSON(path.join(data, "workbench.json"), { projects: [], standaloneDirectories: [shared], defaultDirectory: shared,
  activeConversation: { project: shared, resumeId: "one", scope: "standalone" } });
let app, page; const errors = [];
async function launch() {
  const env = { ...process.env, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1", USERPROFILE: home, HOME: home };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
}
// Legacy history is migrated by the storage actor (a Python process) when the catalog is first read, so
// the sidebar fills in shortly after the window is ready; wait for it rather than counting immediately.
const sessionsShown = (count) => page.waitForFunction((count) => document.querySelectorAll(".standalone-group .session-item").length === count, count);
async function main() {
  try {
    await launch(); await sessionsShown(2);
    const first = await page.locator(".breadcrumbs button").getAttribute("title"); assert.notEqual(first, shared);
    assert.equal(fs.readFileSync(path.join(first, "artifact.txt"), "utf8"), "existing output");
    await page.locator(".inline-file-link").click(); await page.getByRole("region", { name: "文件预览", exact: true }).getByText("existing output", { exact: true }).waitFor();
    await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
    fs.writeFileSync(path.join(first, "artifact.txt"), "first workspace only");
    await page.locator(".standalone-group .session-item").filter({ hasText: "旧独立会话 two" }).click();
    await page.waitForFunction((value) => document.querySelector(".breadcrumbs button")?.title !== value && !document.querySelector(".new-task").disabled, first);
    const second = await page.locator(".breadcrumbs button").getAttribute("title"); assert.notEqual(first, second);
    assert.equal(fs.readFileSync(path.join(second, "artifact.txt"), "utf8"), "existing output");
    assert.equal(fs.readFileSync(path.join(shared, "artifact.txt"), "utf8"), "existing output");
    for (const [directory, id] of [[first, "one"], [second, "two"]]) {
      assert(fs.existsSync(path.join(historyRoot(directory, data), "sessions", id + ".json")));
      assert(!fs.existsSync(path.join(directory, ".xuloom/sessions")));
    }
    assert(!fs.existsSync(path.join(shared, ".xuloom/sessions"))); assert(!fs.existsSync(path.join(shared, ".xuloom/session-index.json")));
    await app.close(); await launch();
    await page.waitForFunction((second) => document.querySelector(".breadcrumbs button")?.getAttribute("title") === second, second);
    await sessionsShown(2);
    const proof = await app.evaluate(async ({ BrowserWindow }) => {
      const web = BrowserWindow.getAllWindows()[0].webContents; await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise((resolve) => setTimeout(resolve, 100)); return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/central-history.png"), Buffer.from(proof, "base64"));
    assert.deepEqual(errors, []); console.log("Storage E2E passed: legacy shared chats isolated, owned files copied without overwriting originals, centralized metadata/history, migrated file preview and stable restart association");
  } catch (error) { if (page) console.error(await page.locator("body").innerText()); throw error; }
  finally { if (app) await app.close().catch(() => {}); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
