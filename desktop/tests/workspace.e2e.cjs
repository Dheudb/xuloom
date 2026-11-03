const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");
const { atomicJSON, projectKey } = require("../electron/catalog.cjs");
const { historyRoot } = require("../electron/history-storage.cjs");
const { defaultWorkspace } = require("../electron/workspace.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-workspace-e2e-"));
const data = path.join(fixture, "profile"), config = path.join(fixture, "config");
const custom = path.join(fixture, "自定义默认目录"), explicit = path.join(fixture, "explicit-project");
for (const dir of [data, config, explicit]) fs.mkdirSync(dir);
const workbench = path.join(data, "workbench.json");
// No projects, workspace selection, or preconfigured default directory.
atomicJSON(workbench, { projects: [], activeConversation: null });
atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "", openai_api_key: "", openai_base_url: "http://127.0.0.1:9/v1" });
const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1",
  LOCALAPPDATA: path.join(fixture, "os-data"), XDG_DATA_HOME: path.join(fixture, "xdg-data") };
delete env.ELECTRON_RUN_AS_NODE; delete env.XULOOM_DEV_URL;
for (const key of ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
const options = { executablePath: process.env.XULOOM_TEST_EXECUTABLE,
  args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 };
let app, page;
const errors = [];
const assertPath = (actual, expected) => assert.equal(projectKey(actual), projectKey(expected));
const readSettings = () => JSON.parse(fs.readFileSync(workbench, "utf8"));
async function launch() {
  app = await electron.launch(options); page = await app.firstWindow(); page.setDefaultTimeout(45000);
  page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function main() {
  try {
    await launch();
    // Stub only the OS home for macOS, keeping all created files in the fixture.
    if (process.platform === "darwin") await app.evaluate(({ app }, home) => app.setPath("home", home), fixture);
    const home = await app.evaluate(({ app }) => app.getPath("home"));
    const defaultDirectory = defaultWorkspace({ home, env: { ...env, XULOOM_DATA_DIR: data } });
    assertPath((await page.evaluate(() => window.xuloom.invoke("workspace.get"))).defaultDirectory, defaultDirectory);
    assert(!fs.existsSync(defaultDirectory), "Startup does not create an unsolicited project");
    // New session must never request an OS folder chooser.
    await app.evaluate(({ dialog }) => { dialog.showOpenDialog = async () => { throw new Error("Unexpected folder chooser"); }; });
    await page.locator(".new-task").click();
    await page.locator(".session-item.active").waitFor();
    const firstWorkspace = await page.locator(".project-context span").getAttribute("title");
    assertPath(path.dirname(firstWorkspace), fs.realpathSync(defaultDirectory));
    fs.writeFileSync(path.join(firstWorkspace, "isolation.txt"), "first conversation only");
    const defaultId = await page.locator(".session-item.active").getAttribute("data-session-id");
    assert.equal(await page.locator(".standalone-group .session-item.active").count(), 1);
    assert.equal(await page.locator(".project-group").count(), 0);
    assert(fs.existsSync(path.join(historyRoot(firstWorkspace, data), "sessions", defaultId.split(":")[1] + ".json")));
    assert(!fs.existsSync(path.join(firstWorkspace, ".xuloom/sessions")));
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    // Directory configuration works independently of the model configuration.
    await page.getByLabel("模型 ID", { exact: true }).fill("");
    assert(await page.getByRole("button", { name: "保存配置", exact: true }).isDisabled());
    await page.getByRole("tab", { name: "工作区", exact: true }).click();
    assertPath(await page.getByLabel("默认工作目录", { exact: true }).inputValue(), defaultDirectory);
    await page.getByLabel("默认工作目录", { exact: true }).fill("relative-directory");
    await page.getByRole("button", { name: "保存默认目录", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "绝对路径" }).waitFor();
    assert.equal(readSettings().defaultDirectory, undefined);
    const originalConfig = fs.readFileSync(path.join(config, "config.json"), "utf8");
    await page.getByLabel("默认工作目录", { exact: true }).fill(custom);
    await page.getByRole("button", { name: "保存默认目录", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "默认目录已保存" }).waitFor();
    assertPath(readSettings().defaultDirectory, fs.realpathSync(custom));
    assert.equal(fs.readFileSync(path.join(config, "config.json"), "utf8"), originalConfig);
    const screenshot = await app.evaluate(async ({ BrowserWindow }) =>
      (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64"));
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
    fs.writeFileSync(path.join(root, "test-results/workspace-settings.png"), Buffer.from(screenshot, "base64"));
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    // Global New always uses the current default, preserving old independent histories.
    await page.locator(".new-task").click();
    await page.waitForFunction((previous) => !document.querySelector(".new-task").disabled &&
      document.querySelector(".session-item.active")?.getAttribute("data-session-id") !== previous, defaultId);
    const customId = await page.locator(".session-item.active").getAttribute("data-session-id");
    const secondWorkspace = await page.locator(".project-context span").getAttribute("title");
    assert.notEqual(secondWorkspace, firstWorkspace); assert(!fs.existsSync(path.join(secondWorkspace, "isolation.txt")));
    fs.writeFileSync(path.join(secondWorkspace, "isolation.txt"), "second conversation only");
    assert.equal(fs.readFileSync(path.join(firstWorkspace, "isolation.txt"), "utf8"), "first conversation only");
    assert.notEqual(customId, defaultId);
    assertPath(path.dirname(await page.locator(".project-context span").getAttribute("title")), fs.realpathSync(custom));
    assert.equal(await page.locator(".standalone-group .session-item").count(), 2);
    await page.getByRole("textbox", { name: "任务输入" }).fill("独立会话的草稿");
    await app.evaluate(({ dialog }, directory) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [directory] });
    }, explicit);
    await page.getByRole("button", { name: "创建项目", exact: true }).click();
    await page.getByRole("button", { name: "添加文件夹", exact: true }).click();
    await page.getByRole("dialog", { name: "创建项目", exact: true }).getByRole("button", { name: "创建项目", exact: true }).click();
    await page.locator(`[data-project="${explicit.replace(/\\/g, "\\\\")}"] .session-item.active`).waitFor();
    await page.getByRole("textbox", { name: "任务输入" }).fill("项目会话的草稿");
    await page.getByRole("button", { name: "在项目 explicit-project 中新建会话", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    assertPath(await page.locator(".project-context span").getAttribute("title"), fs.realpathSync(explicit));
    assert.equal(await page.locator(".project-group .session-item").count(), 2);
    await page.keyboard.press("Control+n");
    await page.waitForFunction(() => !document.querySelector(".new-task").disabled && !!document.querySelector(".standalone-group .session-item.active"));
    assertPath(path.dirname(await page.locator(".project-context span").getAttribute("title")), fs.realpathSync(custom));
    assert.equal(await page.locator(".project-group .session-item").count(), 2);
    assert.equal(await page.locator(".standalone-group .session-item").count(), 3);
    const layout = await app.evaluate(async ({ BrowserWindow }) => {
      const web = BrowserWindow.getAllWindows()[0].webContents;
      await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise((resolve) => setTimeout(resolve, 100));
      return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.writeFileSync(path.join(root, "test-results/standalone-sessions.png"), Buffer.from(layout, "base64"));
    await page.evaluate((directory) => window.xuloom.invoke("project.remove", { path: directory, confirm: true }), explicit);
    await page.waitForFunction(() => document.querySelectorAll(".project-group").length === 0);
    assert.equal(await page.locator(".standalone-group .session-item").count(), 3);
    assert(fs.existsSync(path.join(historyRoot(firstWorkspace, data), "sessions", defaultId.split(":")[1] + ".json")), "Default directory changes preserve centralized histories");

    await app.close(); await launch();
    assertPath(path.dirname(await page.locator(".project-context span").getAttribute("title")), fs.realpathSync(custom));
    assert.equal(await page.locator(".standalone-group .session-item.active").count(), 1);
    assert.equal(await page.locator(".standalone-group .session-item").count(), 3);
    assert.equal(readSettings().activeConversation.scope, "standalone");
    const independentId = await page.locator(".session-item.active").getAttribute("data-session-id");
    await page.evaluate((id) => window.xuloom.invoke("session.update", { id, patch: { title: "独立归档测试" } }), independentId);
    await page.getByRole("button", { name: "会话操作 独立归档测试", exact: true }).click();
    await page.getByRole("menuitem", { name: "归档会话", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".standalone-group .session-item").length === 2);
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.getByRole("tab", { name: "已归档会话", exact: true }).click();
    await page.getByRole("button", { name: "恢复会话 独立归档测试", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "已恢复会话" }).waitFor();
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    assert.equal(await page.locator(".standalone-group .session-item").count(), 3);
    assert.equal(await page.locator(".project-group").count(), 0);
    assert.deepEqual(errors, []);
    console.log("Workspace E2E passed: global New/Ctrl+N independent, project + scoped, default-directory changes, drafts/history preserved, project removal and restart");
  } catch (error) {
    console.error("Fixture:", fixture);
    if (page && !page.isClosed()) console.error("Workspace UI:", await page.locator("body").innerText().catch(() => "unavailable"));
    throw error;
  } finally { if (app) await app.close().catch(() => {}); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
