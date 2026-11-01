const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-capture-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "data"), config = path.join(fixture, "config");
for (const dir of [project, data, config]) fs.mkdirSync(dir);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe") : path.join(root, "../.venv-mcp/Scripts/python.exe");
let app, page, endpoint, captures = [], requests = [], errors = [];
const html = `<!doctype html><html><head><title>CAPTURE_FIXTURE</title><link rel="stylesheet" href="style.css"></head><body><h1>后台渲染验证</h1><svg width="100" height="100"><circle cx="50" cy="50" r="40" fill="gold"/></svg><script>console.error('CAPTURE_CONSOLE_PROOF');document.title += ':'+[typeof require,typeof process,typeof window.xuloom].join(',');</script></body></html>`;
fs.writeFileSync(path.join(project, "page.html"), html);
fs.writeFileSync(path.join(project, "style.css"), "html,body{margin:0;min-height:100%;background:rgb(170,187,204)}h1{padding:40px}");
fs.writeFileSync(path.join(fixture, "outside.html"), "outside");
atomicJSON(path.join(project, ".xuloom/sessions/capture.json"), { messages: [], metadata: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [project], workspace: [{ project, resumeId: "capture", active: true }] });
const server = http.createServer((req, res) => {
  if (!req.url.startsWith("/v1/")) {
    requests.push({ path: req.url, cookie: req.headers.cookie });
    if (req.url === "/preview") return res.writeHead(200, { "Content-Type": "text/html" }).end('<html><title>REMOTE_FIXTURE</title><body style="margin:0;background:#aabbcc">Local development server</body></html>');
    return res.writeHead(404).end("missing");
  }
  let raw = ""; req.on("data", c => raw += c);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      const user = [...body.messages].reverse().find(m => m.role === "user" && typeof m.content === "string" && m.content.startsWith("CAPTURE_"));
      const after = body.messages.slice(body.messages.indexOf(user) + 1), tools = after.filter(m => m.role === "tool");
      res.writeHead(200, { "Content-Type": "text/event-stream" });
      const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
      let call;
      if (!(body.tools || []).some(t => t.function.name === "browser_tool")) call = ["tool_search_tool", { query: "hidden browser screenshot", activate: ["browser_tool"] }];
      else if (!tools.some(m => m.tool_call_id === "render")) {
        const common = { action: "render", width: 800, height: 600, wait_ms: user.content === "CAPTURE_CANCEL" ? 10000 : 0, full_page: false };
        call = ["browser_tool", { ...common, ...(user.content === "CAPTURE_LOCAL" ? { file_path: path.join(project, "page.html") } : { url: endpoint + (user.content === "CAPTURE_MISSING" ? "/missing" : "/preview") }) }];
      } else {
        const text = JSON.stringify(after);
        const images = after.flatMap(m => Array.isArray(m.content) ? m.content.filter(c => c.type === "image_url") : []);
        if (user.content !== "CAPTURE_MISSING") {
          assert(images.length > 0, "screenshot must reach model input as an image");
          const encoded = images[0].image_url.url.split(",")[1];
          const file = path.join(fixture, user.content + ".png"); fs.writeFileSync(file, Buffer.from(encoded, "base64"));
          // Inspect actual rendered pixels, not just tool status or a saved path.
          execFileSync(python, ["-I", "-c", "from PIL import Image;import sys;im=Image.open(sys.argv[1]).convert('RGB');assert im.size==(800,600),im.size;assert max(abs(a-b) for a,b in zip(im.getpixel((790,590)),(170,187,204)))<=5,im.getpixel((790,590))", file], { windowsHide: true });
          if (user.content === "CAPTURE_LOCAL") assert(text.includes("CAPTURE_FIXTURE:undefined,undefined,undefined"), "rendered page must not have Node or desktop bridge");
        }
        if (user.content === "CAPTURE_LOCAL") assert(text.includes("CAPTURE_CONSOLE_PROOF"), "renderer console errors must reach the receipt");
        captures.push({ task: user.content, images: images.length, text });
        chunk({ role: "assistant", content: "截图检查已完成。" }); chunk({}, "stop");
      }
      if (call) { const [name, input] = call; chunk({ role: "assistant", tool_calls: [{ index: 0, id: name === "browser_tool" ? "render" : "discover", type: "function", function: { name, arguments: JSON.stringify(input) } }] }); chunk({}, "tool_calls"); }
      res.end("data: [DONE]\n\n");
    } catch (error) { errors.push(error.stack); res.end(); }
  });
});
async function launch() {
  const env = { ...process.env, HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on("pageerror", e => errors.push(e.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) { await page.getByRole("textbox", { name: "任务输入" }).fill(text); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function finished() { await page.waitForFunction(() => !document.querySelector(".send-button.stop")); }
async function main() {
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve)); endpoint = `http://127.0.0.1:${server.address().port}`;
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "capture-fixture", openai_api_key: "fixture", openai_base_url: endpoint + "/v1" });
    await launch(); await page.locator(".session-item.active").waitFor();
    await app.evaluate(async ({ session }, url) => session.defaultSession.cookies.set({ url, name: "private-login", value: "never-share" }), endpoint);
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("CAPTURE_LOCAL"); await finished();
    assert.equal(captures.length, 1, JSON.stringify(errors));
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1, "capture window must be destroyed");
    await page.locator(".turn-process").last().locator(":scope > summary").click();
    const image = page.locator(".turn-process").last().locator(".tool-image-thumb").first();
    await image.scrollIntoViewIfNeeded(); await image.locator("img").waitFor();
    await image.click(); await page.getByRole("dialog", { name: "图片附件预览" }).waitFor();
    await page.keyboard.press("Escape");
    await send("CAPTURE_REMOTE"); await finished(); assert.equal(captures.length, 2, JSON.stringify(errors));
    assert(requests.some(r => r.path === "/preview")); assert(requests.every(r => !r.cookie));
    // Deny files outside the workspace, unsafe protocols and HTTP error pages.
    const failures = await app.evaluate(async ({ app }, { project, endpoint }) => {
      const load = process.getBuiltinModule("node:module").createRequire(app.getAppPath() + "/package.json");
      const { BrowserCaptures } = load("./electron/browser-capture.cjs");
      const captures = new BrowserCaptures(), results = [];
      for (const input of [{ file_path: "../outside.html" }, { url: "file:///C:/Windows/win.ini" }, { url: endpoint + "/missing" }]) {
        try { await captures.capture("fixture", project, String(results.length), input); results.push("unexpected success"); }
        catch (error) { results.push(error.message); }
      }
      return { results, pending: captures.active.size };
    }, { project, endpoint });
    assert(failures.results.every(r => r !== "unexpected success"), JSON.stringify(failures));
    assert.match(failures.results[2], /404/); assert.equal(failures.pending, 0);
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
    fs.writeFileSync(path.join(project, "long.html"), '<html><body style="margin:0;background:#aabbcc;height:1800px"><div style="position:absolute;top:1700px;width:100%;height:100px;background:#123456">bottom</div></body></html>');
    const selected = await page.locator('.session-item.active').getAttribute('data-session-id');
    const full = await app.evaluate(async ({ app, BrowserWindow }, {project, selected}) => {
      const load = process.getBuiltinModule("node:module").createRequire(app.getAppPath() + "/package.json");
      const web = BrowserWindow.getAllWindows()[0].webContents;
      const transport = { request: (method, params) => web.executeJavaScript('window.xuloom.invoke('+JSON.stringify(method)+','+JSON.stringify({...params,session:selected})+')') };
      return new (load("./electron/browser-capture.cjs").BrowserCaptures)().capture("fixture", project, "full", { file_path: "long.html", width: 800, height: 600, full_page: true, wait_ms: 0 }, transport);
    }, {project, selected});
    const fullFile = path.join(fixture, "full-page.png"); fs.writeFileSync(fullFile, Buffer.from(full.png_base64, "base64"));
    assert.equal(full.height, 1800); assert.equal(full.truncated, false);
    // Chromium/runtime display profiles shift dark colors slightly. Keep exact
    // dimensions and require the distinct bottom marker, with a 12/255 tolerance.
    execFileSync(python, ["-I", "-c", "from PIL import Image;import sys;im=Image.open(sys.argv[1]).convert('RGB');assert im.size==(800,1800),im.size;assert max(abs(a-b) for a,b in zip(im.getpixel((400,1750)),(18,52,86)))<=12,im.getpixel((400,1750))", fullFile], { windowsHide: true });
    await send("CAPTURE_CANCEL");
    await page.waitForFunction(() => document.querySelector(".send-button.stop"));
    for (let i = 0; i < 100 && await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length) < 2; i++) await new Promise(r => setTimeout(r, 30));
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some(w => w.isVisible())), false);
    await page.getByRole("button", { name: "停止任务", exact: true }).click(); await finished();
    for (let i = 0; i < 100 && await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length) > 1; i++) await new Promise(r => setTimeout(r, 30));
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
    await app.close(); await launch();
    await page.locator(".turn-process").first().locator(":scope > summary").click();
    const restored = page.locator(".turn-process").first().locator(".tool-image-thumb").first();
    await restored.scrollIntoViewIfNeeded(); await restored.locator("img").waitFor();
    assert.deepEqual(errors, []);
    console.log("Browser capture E2E passed: hidden Chromium, local HTML/CSS pixels, local HTTP, native model image input, tool thumbnail/enlargement, cancellation cleanup and history restoration; cookie/Node isolation, path/protocol/HTTP-error denial; no external API.");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
