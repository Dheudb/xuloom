const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { atomicJSON, sessionKey } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-git-ui-"));
const project = path.join(fixture, "repo"), plain = path.join(fixture, "plain"), data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [project, plain, data, config]) fs.mkdirSync(dir);
const git = (...args) => execFileSync("git", args, { cwd: project, windowsHide: true, stdio: "pipe" });
git("init", "-b", "main");
for (const [dir, id] of [[project, "repo"], [plain, "plain"]]) atomicJSON(path.join(dir, ".xuloom/sessions/" + id + ".json"), { messages: [{ role: "user", content: id + " conversation" }], metadata: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [project, plain], workspace: [{ project, resumeId: "repo", active: true }] });
let app, page, requests = 0; const errors = [];
const server = http.createServer((_req, res) => { requests++; res.writeHead(500).end("Git reads must not call model APIs"); });
async function launch() {
  const env = { ...process.env, HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", e => errors.push(e.message));
  await page.locator(".session-item.active").waitFor();
}
async function main() {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch();
    const version = await app.evaluate(({ app }) => app.getVersion());
    assert((await page.locator(".version").innerText()).startsWith("v" + version));
    const button = () => page.getByRole("button", { name: "Git 状态", exact: true });
    await button().getByText("main", { exact: true }).waitFor();
    assert((await button().getAttribute("title")).includes("尚无提交"));
    fs.writeFileSync(path.join(project, "note.md"), "one\n");
    git("add", "note.md"); git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline");
    fs.writeFileSync(path.join(project, "note.md"), "two\n");
    await button().click();
    await page.waitForFunction(() => document.querySelector('.git-status')?.getAttribute('title').includes('未暂存 1'));
    await button().getByLabel("有未提交变更").waitFor();
    const branch = "feature/" + "long-name-".repeat(9); git("checkout", "-b", branch);
    await button().click(); await button().getByText(branch, { exact: true }).waitFor();
    assert((await button().boundingBox()).width <= 170.5);
    assert((await button().boundingBox()).height <= 32, "branch badge must remain compact");
    git("checkout", "--detach"); await button().click();
    await page.waitForFunction(() => document.querySelector('.git-status')?.textContent.includes('HEAD ·'));
    await page.locator(`.session-item[data-session-id="${sessionKey(plain, "plain")}"] .session-select`).click();
    await page.waitForFunction(() => !document.querySelector('.git-status'));
    await page.locator(`.session-item[data-session-id="${sessionKey(project, "repo")}"] .session-select`).click();
    await button().waitFor();
    const screenshot = await app.evaluate(async ({ BrowserWindow }) => {
      const web = BrowserWindow.getAllWindows()[0].webContents;
      await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
      await new Promise(resolve => setTimeout(resolve, 100));
      return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
    });
    fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/git-status.png"), Buffer.from(screenshot, "base64"));
    await app.close(); await launch();
    await page.waitForFunction(() => document.querySelector('.git-status')?.textContent.includes('HEAD ·'));
    assert.equal(requests, 0); assert.deepEqual(errors, []);
    console.log("Git status E2E passed: unborn/dirty/long branch/detached, workspace switch, restart, bounded layout; no model calls");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
