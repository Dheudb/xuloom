const { _electron: electron } = require("playwright");
const fs = require("node:fs"), os = require("node:os"), path = require("node:path"), assert = require("node:assert/strict"), http = require("node:http");
const { SessionCatalog, atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-collaboration-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [data, config]) fs.mkdirSync(dir, { recursive: true });
let settings = {};
const catalog = new SessionCatalog(() => settings, (value) => { settings = value; }, data);
const first = catalog.createStandalone(fixture);
catalog.update(first, { title: "Collaboration A" });
settings.activeConversation = { project: first.project, resumeId: first.sessionId, scope: "standalone" };
atomicJSON(path.join(data, "workbench.json"), settings);
const requests = [], errors = [];
const requestWaiters = new Set();
function nextRequest() {
  return new Promise((resolve, reject) => {
    const receive = () => { clearTimeout(timer); requestWaiters.delete(receive); resolve(); };
    const timer = setTimeout(() => { requestWaiters.delete(receive); reject(new Error("Local request did not arrive")); }, 30000);
    timer.unref(); requestWaiters.add(receive);
  });
}
let app, page, pending;
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (part) => { raw += part; }); req.on("end", () => {
    requests.push(JSON.parse(raw)); pending = res;
    for (const receive of [...requestWaiters]) receive();
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write('data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n');
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER", "XULOOM_COLLABORATION_MODE"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
const picker = () => page.getByLabel("协作档位", { exact: true });
async function capture(name) {
  const png = await app.evaluate(async ({ BrowserWindow }) => {
    const contents = BrowserWindow.getAllWindows()[0].webContents;
    await contents.capturePage(undefined, { stayHidden: true, stayAwake: true });
    return (await contents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
  });
  fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
  fs.writeFileSync(path.join(root, "test-results", name), Buffer.from(png, "base64"));
}
async function controlsFit() {
  assert.equal(await page.locator(".composer-toolbar").evaluate(el => el.scrollWidth > el.clientWidth), false);
  const size = await page.getByLabel("选择模型", { exact: true }).evaluate(element => {
    const style = getComputedStyle(element), context = document.createElement("canvas").getContext("2d");
    context.font = style.font;
    return { actual: element.getBoundingClientRect().width, text: context.measureText(element.selectedOptions[0].text).width, cap: parseFloat(style.maxWidth) };
  });
  assert(size.actual >= Math.min(size.cap, size.text + 16) - 1, "The selected model name must not be squeezed away: " + JSON.stringify(size));
  assert(size.actual <= Math.min(size.cap, size.text + 40) + 1, "Short model names must not leave a fixed-width blank band");
  const effort = page.getByLabel("推理强度", { exact: true });
  if (await effort.count()) assert((await effort.boundingBox()).width <= 110, "Effort must not inherit a form's full width");
}
async function choose(mode) {
  await picker().selectOption(mode);
  await page.waitForFunction((mode) => { const el = document.querySelector(".collaboration-picker"); return el.value === mode && !el.disabled; }, mode);
}
(async () => {
  try {
    await launch();
    assert.equal(await picker().inputValue(), "auto");
    // Local preference does not depend on an API being configured.
    await choose("solo");
    assert.equal(await page.getByLabel("访问权限", { exact: true }).inputValue(), "ask");
    assert.equal(JSON.parse(fs.readFileSync(catalog.file(first.project, first.sessionId))).metadata.collaboration_mode, "solo");
    await app.close(); app = null;
    await launch(); assert.equal(await picker().inputValue(), "solo");
    await choose("ultra");
    const originalId = await page.locator(".session-item.active").getAttribute("data-session-id");
    await page.locator(".new-task").click();
    await page.waitForFunction((id) => document.querySelector(".session-item.active")?.getAttribute("data-session-id") !== id && document.querySelector(".new-task")?.disabled === false, originalId);
    assert.equal(await picker().inputValue(), "ultra", "A new conversation inherits the last explicitly selected policy");
    await page.locator(".session-item").filter({ hasText: "Collaboration A" }).click();
    await page.waitForFunction(() => document.querySelector(".collaboration-picker")?.value === "ultra");
    await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1100, 850));
    assert.equal(await page.locator(".composer-toolbar").evaluate((el) => el.scrollWidth > el.clientWidth), false, "Options and stop/send stay accessible in a narrow conversation");
    await app.close(); app = null;
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${server.address().port}/v1`;
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", active_model_connection: "fixture", model_connections: [{
      id: "fixture", name: "Fixture API", provider: "openai", model: "collaboration-fixture", models: ["collaboration-fixture", "unknown-fixture", "tiny", "very-long-model-identifier-that-should-use-the-width-cap"],
      api_key: "fixture", base_url: base, model_reasoning: { "collaboration-fixture": { adapter: "deepseek", efforts: ["low", "high", "max"],
        canDisable: true, thinking: "default", effort: "high", budgetTokens: 2048 } },
    }] });
    await launch();
    assert.deepEqual(await page.getByLabel("Thinking 模式", { exact: true }).locator("option").allTextContents(), ["开启思考", "关闭思考"]);
    assert.equal(await page.getByLabel("Thinking 模式", { exact: true }).inputValue(), "enabled", "Legacy default thinking must show an explicit enabled state");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.locator('.model-options > summary').click();
    const efforts = page.getByRole("group", { name: "模型支持的推理强度", exact: true });
    await efforts.scrollIntoViewIfNeeded();
    const gap = await efforts.evaluate(element => element.querySelector("label").getBoundingClientRect().top - element.querySelector("span").getBoundingClientRect().bottom);
    assert(gap >= 0 && gap <= 12, "Checkboxes must sit directly below their label, without a blank band");
    assert(await efforts.locator("label").first().evaluate(element => ["inline-flex", "flex"].includes(getComputedStyle(element).display)));
    await capture("reasoning-settings-compact.png");
    await page.keyboard.press("Escape");
    await page.getByLabel("Thinking 模式", { exact: true }).selectOption("disabled");
    await page.waitForFunction(() => document.querySelector(".thinking-picker")?.value === "disabled" && !document.querySelector(".thinking-picker").disabled);
    assert(await page.getByLabel("推理强度", { exact: true }).isDisabled());
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1100, 850));
    await controlsFit();
    await capture("reasoning-toolbar-compact.png");
    assert.equal(await page.locator(".composer-toolbar").evaluate(el => el.scrollWidth > el.clientWidth), false, "Thinking controls must fit a narrow composer");
    const firstRequest = nextRequest();
    await page.getByRole("textbox", { name: "任务输入" }).fill("Hold for stop test");
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByRole("button", { name: "停止任务", exact: true }).waitFor();
    await firstRequest;
    assert.equal(await picker().isDisabled(), true);
    const rejected = await page.evaluate(async (id) => { try {
      await window.xuloom.invoke("collaboration.set", { session: id, mode: "solo" }); return "unexpected success";
    } catch (error) { return String(error); } }, originalId);
    assert(rejected.includes("当前任务"), rejected);
    await page.getByRole("button", { name: "停止任务", exact: true }).click();
    await picker().waitFor(); await page.waitForFunction(() => !document.querySelector(".collaboration-picker").disabled);
    assert(requests.length > 0 && JSON.stringify(requests[0]).includes("# Collaboration: ultra"));
    assert(JSON.stringify(requests[0].messages).includes("use ultra_prepare for research"));
    const system = requests[0].messages.filter((message) => message.role === "system").map((message) => message.content).join("\n");
    assert.equal(system.split("# Collaboration: ultra").length - 1, 1);
    assert(system.indexOf("# Collaboration: ultra") > system.indexOf("# Tool-Specific Rules"));
    assert(system.includes("continue the main work yourself"));
    assert(!JSON.stringify(requests[0].messages).includes("run_in_background=false when the current answer requires"));
    const names = requests[0].tools.map(tool=>tool.function.name);
    assert.deepEqual([...names].sort(),['ultra_prepare']);
    assert(!names.includes('collaboration_decision'));
    assert.equal(requests[0].tool_choice, 'auto', 'Ultra validates assessment in the loop without forcing unsupported thinking parameters');
    assert(!system.includes('# Ultra collaboration decision (this request only)'));
    const roles = requests[0].tools.find(tool=>tool.function.name==='ultra_prepare').function.parameters.$defs.Assignment.properties.subagent_type.anyOf.find(option=>option.enum).enum;
    for (const role of ["explore", "general-purpose", "plan", "verification", "software-engineer"]) assert(roles.includes(role));
    assert(!roles.includes("research"));
    assert.deepEqual(requests[0].thinking, { type: "disabled" });
    assert.equal(requests[0].reasoning_effort, undefined, "Disabled thinking must omit conflicting effort");
    await page.getByLabel("Thinking 模式", { exact: true }).selectOption("enabled");
    await page.getByLabel("推理强度", { exact: true }).selectOption("max");
    await page.waitForFunction(() => document.querySelector(".reasoning-picker")?.value === "max" && !document.querySelector(".reasoning-picker").disabled);
    const enabledRequest = nextRequest();
    await page.getByRole("textbox", { name: "任务输入" }).fill("Enabled thinking wire check");
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[aria-label="停止任务"]'));
    await enabledRequest;
    await page.getByRole("button", { name: "停止任务", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".collaboration-picker").disabled);
    assert.deepEqual(requests.at(-1).thinking, { type: "enabled" });
    assert.equal(requests.at(-1).reasoning_effort, "max");
    await page.getByLabel("选择模型", { exact: true }).selectOption(JSON.stringify(["fixture", "unknown-fixture"]));
    await page.getByLabel("Thinking 模式", { exact: true }).waitFor({ state: "detached" });
    assert.equal(await page.getByLabel("推理强度", { exact: true }).count(), 0, "Unknown models must not inherit another model's controls");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.locator('.model-options > summary').click();
    await page.getByLabel("推理参数格式", { exact: true }).selectOption("qwen-local");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "工作台设置", exact: true }).waitFor({ state: "hidden" });
    await page.getByLabel("Thinking 模式", { exact: true }).waitFor();
    assert.equal(await page.getByLabel("推理强度", { exact: true }).count(), 0);
    const saved = JSON.parse(fs.readFileSync(path.join(config, "config.json"), "utf8"));
    assert.equal(saved.model_connections[0].model_reasoning["unknown-fixture"].adapter, "qwen-local");
    assert.equal(saved.model_connections[0].model_reasoning["collaboration-fixture"].effort, "high", "Changing effort in one conversation must not overwrite the shared default");
    const sessionChoice = JSON.parse(fs.readFileSync(catalog.file(first.project, first.sessionId))).metadata.model_selection;
    assert.equal(sessionChoice.reasoning.fixture["collaboration-fixture"].effort, "max", "The original model's preference stays with this conversation");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.locator('.model-options > summary').click();
    await page.getByLabel("推理参数格式", { exact: true }).selectOption("openai");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "工作台设置", exact: true }).waitFor({ state: "hidden" });
    assert.equal(await page.getByLabel("Thinking 模式", { exact: true }).count(), 0);
    await controlsFit();
    await capture("reasoning-effort-only.png");
    await page.getByLabel("选择模型", { exact: true }).selectOption(JSON.stringify(["fixture", "tiny"]));
    await page.waitForFunction(() => document.querySelector(".model-picker")?.title.endsWith("tiny") && !document.querySelector(".model-picker").disabled);
    await controlsFit();
    assert((await page.getByLabel("选择模型", { exact: true }).boundingBox()).width < 100);
    await capture("model-picker-short-name.png");
    await page.getByLabel("选择模型", { exact: true }).selectOption(JSON.stringify(["fixture", "very-long-model-identifier-that-should-use-the-width-cap"]));
    await page.waitForFunction(() => document.querySelector(".model-picker")?.title.includes("very-long-model") && !document.querySelector(".model-picker").disabled);
    await capture("model-picker-long-name.png");
    await controlsFit();
    assert((await page.getByLabel("选择模型", { exact: true }).boundingBox()).width <= 240);
    await choose("solo");
    assert.deepEqual(errors, []);
    console.log("Collaboration/reasoning E2E passed: mode persistence, native spawn schema, model-specific settings, SDK wire parameters, unknown-model reset, narrow layout and stop; hidden local fixture only");
  } finally {
    pending?.end(); if (app) await app.close().catch(() => {});
    server.closeAllConnections(); server.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
