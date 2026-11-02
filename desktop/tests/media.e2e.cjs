const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto"), { spawnSync } = require("node:child_process");
const { atomicJSON } = require("../electron/catalog.cjs"), { resolveEnginePython } = require("../electron/runtime.cjs");
const root = path.resolve(__dirname, ".."), repo = path.dirname(root), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-media-e2e-"));
const config = path.join(fixture, "config"), data = path.join(fixture, "data"), work = path.join(fixture, "work");
for (const directory of [config, data, work]) fs.mkdirSync(directory);
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null, defaultDirectory: work });
const env = { ...process.env, XULOOM_CONFIG_DIR: config, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, HOME: path.join(fixture, "home"), USERPROFILE: path.join(fixture, "home"), XULOOM_SKILLS_PATHS: "", XULOOM_TEST_HEADLESS: "1", NO_PROXY: "127.0.0.1,localhost", no_proxy: "127.0.0.1,localhost" };
for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
let base, app, page, mediaConnectionId, sequence = 0, video, png, pending = false, statusRequests = 0;
const jobs = new Map(), posts = [], chatRequests = [], errors = [];
const wav = Buffer.alloc(44 + 32000);
wav.write("RIFF", 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8); wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write("data", 36); wav.writeUInt32LE(32000, 40);
function json(res, value) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(value)); }
const server = http.createServer((req, res) => {
  if (req.method === 'PUT' && req.url.endsWith('/cancel')) {
    assert.equal(req.headers.authorization, 'Key MEDIA_FIXTURE_KEY'); json(res, { status: 'OK' }); return;
  }
  if (req.method === "GET") {
    if (req.url.startsWith("/asset/")) {
      const kind = req.url.split("/").at(-1), body = kind === "image" ? png : kind === "video" ? video : wav;
      assert(!req.headers.authorization, "Generated media downloads must not receive the API key");
      res.writeHead(200, { "Content-Type": kind === "image" ? "image/png" : kind === "video" ? "video/webm" : "audio/wav", "Content-Length": body.length }).end(body); return;
    }
    const id = req.url.endsWith("/status") ? req.url.split("/").at(-2) : req.url.split("/").at(-1);
    assert.equal(req.headers.authorization, "Key MEDIA_FIXTURE_KEY");
    if (req.url.endsWith("/status")) { statusRequests++; json(res, { request_id: id, status: pending ? "IN_PROGRESS" : "COMPLETED" }); }
    else { const kind = jobs.get(id).fixture_kind; json(res, kind === "image" ? { images: [{ url: base + "/asset/image" }] } : { [kind === "video" ? "video" : "audio"]: { url: base + "/asset/" + kind } }); }
    return;
  }
  let raw = ""; req.on("data", chunk => raw += chunk); req.on("end", () => {
    const body = JSON.parse(raw);
    if (req.url.startsWith("/q/")) {
      assert.equal(req.headers.authorization, "Key MEDIA_FIXTURE_KEY"); posts.push(body);
      const id = randomUUID(); jobs.set(id, body); const url = base + "/q/fal-ai/app/requests/" + id;
      json(res, { request_id: id, status: "IN_QUEUE", status_url: url + "/status", response_url: url, cancel_url: url + "/cancel" }); return;
    }
    assert.equal(req.headers.authorization, "Bearer CHAT_FIXTURE_KEY"); chatRequests.push(body);
    if (!body.stream) { json(res, { choices: [{ message: { role: "assistant", content: "memory" } }] }); return; }
    const human = [...body.messages].reverse().find(message => message.role === "user" && typeof message.content === "string" && message.content.startsWith("MEDIA_"));
    const after = human ? body.messages.slice(body.messages.indexOf(human) + 1).filter(message => message.role === "tool") : [];
    const kind = (human?.content.split("_")[1] || "image").toLowerCase();
    let tool;
    if (after.length === 0) tool = { name: "tool_search_tool", arguments: { query: "generate image video speech music", activate: ["media_tool"] } };
    if (after.length === 1) tool = { name: "media_tool", arguments: { action: "capabilities" } };
    if (after.length === 2) {
      assert(mediaConnectionId && JSON.stringify(after[1]).includes(mediaConnectionId), "Capabilities must expose the saved generated identity to the model");
      tool = { name: "media_tool", arguments: { action: "submit", connection: mediaConnectionId, model: kind, prompt: "Fixture " + kind + " generation", wait_ms: 0 } };
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const send = (delta, finish_reason) => res.write("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (tool) { send({ tool_calls: [{ index: 0, id: "media-ui-" + ++sequence, type: "function", function: { name: tool.name, arguments: JSON.stringify(tool.arguments) } }] }, null); send({}, "tool_calls"); }
    else { send({ content: "MEDIA_DONE_" + kind.toUpperCase() }, null); send({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on("pageerror", error => errors.push(error.message));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
  await page.waitForFunction(() => !document.querySelector(".new-task")?.disabled);
}
async function settings() { await page.getByRole("button", { name: "设置与模型", exact: true }).click(); await page.getByRole("tab", { name: "媒体生成", exact: true }).click(); await page.waitForFunction(() => document.querySelector('.media-settings')?.getAttribute('aria-busy') === 'false'); }
async function run(kind) {
  await page.getByRole("textbox", { name: "任务输入", exact: true }).fill("MEDIA_" + kind.toUpperCase());
  await page.getByRole("button", { name: "发送任务", exact: true }).click(); await page.getByText("MEDIA_DONE_" + kind.toUpperCase(), { exact: true }).last().waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
async function preview(extension) {
  await page.getByRole("button", { name: "输出与来源", exact: true }).click();
  const summary = page.getByRole("dialog", { name: "输出与来源摘要", exact: true });
  await summary.getByRole("button", { name: new RegExp(`media-[a-f0-9]+-1\\.${extension}$`) }).click();
  return page.getByRole("region", { name: "文件预览", exact: true });
}
(async () => { try {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve)); base = `http://127.0.0.1:${server.address().port}`;
  atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "media-ui-fixture", openai_api_key: "CHAT_FIXTURE_KEY", openai_base_url: base + "/v1" });
  await launch();
  const encoded = await page.evaluate(async () => {
    const canvas = document.createElement("canvas"); canvas.width = 96; canvas.height = 64;
    const context = canvas.getContext("2d"); context.fillStyle = "#5b81a8"; context.fillRect(0, 0, 96, 64);
    const png = canvas.toDataURL("image/png").split(",")[1], stream = canvas.captureStream(0), chunks = [];
    const recorder = new MediaRecorder(stream, { mimeType: "video/webm;codecs=vp8" });
    recorder.ondataavailable = event => chunks.push(event.data);
    const stopped = new Promise(resolve => recorder.onstop = resolve); recorder.start(100);
    for (let frame = 0; frame < 10; frame++) { context.fillStyle = frame % 2 ? "#81a8bf" : "#5b81a8"; context.fillRect(0, 0, 96, 64); stream.getVideoTracks()[0].requestFrame(); await new Promise(resolve => setTimeout(resolve, 100)); }
    recorder.stop(); await stopped; stream.getTracks().forEach(track => track.stop());
    const bytes = new Uint8Array(await new Blob(chunks, { type: "video/webm" }).arrayBuffer());
    return { png, video: btoa(String.fromCharCode(...bytes)) };
  });
  png = Buffer.from(encoded.png, "base64"); video = Buffer.from(encoded.video, "base64"); assert(video.includes(Buffer.from([0x1f, 0x43, 0xb6, 0x75])), "The hidden test recorder must produce actual video frames, not only a WebM header");
  await settings(); await page.getByRole("button", { name: "添加生成服务", exact: true }).click();
  assert.equal(await page.getByRole("textbox", { name: "媒体连接 ID", exact: true }).count(), 0);
  assert.equal(await page.getByRole("textbox", { name: "媒体 API 地址", exact: true }).inputValue(), "");
  for (const label of ["图片", "图片编辑", "视频", "语音", "音效", "音乐"])
    await page.locator(".media-add-models").getByRole("button", { name: label, exact: true }).click();
  for (let index = 1; index <= 6; index++) {
    assert.equal(await page.getByLabel("供应商模型端点 " + index, { exact: true }).inputValue(), "");
    assert.equal(await page.getByLabel("媒体默认参数 " + index, { exact: true }).inputValue(), "{}");
  }
  assert(!/minimax|fal-ai\/flux/i.test(await page.locator(".media-settings").innerText()));
  await page.locator(".media-settings h3").scrollIntoViewIfNeeded();
  const blankSettings = await app.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toDataURL());
  fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
  fs.writeFileSync(path.join(root, "test-results/media-blank-config.png"), Buffer.from(blankSettings.split(",")[1], "base64"));
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "添加生成服务", exact: true }).click();
  await page.getByRole("textbox", { name: "媒体连接名称", exact: true }).fill("Generation fixture");
  await page.getByRole("textbox", { name: "媒体 API 地址", exact: true }).fill(base + "/q"); await page.getByLabel("媒体 API Key", { exact: true }).fill("MEDIA_FIXTURE_KEY");
  for (const [index, kind, label] of [[1, "image", "图片"], [2, "speech", "语音"], [3, "video", "视频"]]) {
    await page.locator(".media-add-models").getByRole("button", { name: label, exact: true }).click();
    await page.getByRole("textbox", { name: "供应商模型端点 " + index, exact: true }).fill("fixture-owner/" + kind);
    const row = page.locator(".media-model").nth(index - 1); await row.locator("summary").click();
    await page.getByRole("textbox", { name: "媒体默认参数 " + index, exact: true }).fill(JSON.stringify({ fixture_kind: kind }));
  }
  await page.getByRole("button", { name: "保存生成服务", exact: true }).click(); await page.getByText("生成服务已保存。可直接在对话中描述要生成的内容。", { exact: true }).waitFor();
  assert.equal(posts.length, 0, "Saving configuration must not generate media");
  await page.getByRole("button", { name: "编辑服务", exact: true }).click(); assert.equal(await page.getByLabel("媒体 API Key", { exact: true }).inputValue(), "");
  await page.getByRole("button", { name: "保存生成服务", exact: true }).click(); await page.getByText("Key 已保存", { exact: true }).waitFor();
  const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe") : resolveEnginePython({ packaged: false, projectRoot: repo });
  const cli = spawnSync(python, ["-m", "xuloom", "media", "connections", "--json"], { cwd: repo, env, encoding: "utf8", windowsHide: true });
  assert.equal(cli.status, 0, cli.stderr); const publicConfig = JSON.parse(cli.stdout).result.connections;
  assert.equal(publicConfig[0].models.length, 3); assert(publicConfig[0].hasKey); assert(!cli.stdout.includes("MEDIA_FIXTURE_KEY"));
  assert.match(publicConfig[0].id, /^media-[0-9a-f]{32}$/);
  mediaConnectionId = publicConfig[0].id;
  await page.getByRole("button", { name: "关闭设置", exact: true }).click(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
  await page.getByLabel("访问权限", { exact: true }).selectOption("full"); await page.waitForFunction(() => !document.querySelector(".permission-picker")?.disabled);
  await run("image"); let region = await preview("png"); await region.getByAltText("文件图片预览", { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('.media-preview img')?.naturalWidth > 0);
  await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
  await run("speech"); region = await preview("wav"); await region.getByLabel("音频预览", { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector("audio")?.duration > 1);
  await page.locator("audio").evaluate(async element => { element.muted = true; await element.play(); });
  await page.waitForFunction(() => document.querySelector("audio")?.currentTime > 0);
  await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
  await run("video"); region = await preview("webm"); await region.getByLabel("视频预览", { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector("video")?.videoWidth === 96);
  await page.locator("video").evaluate(async element => { element.muted = true; await element.play(); });
  await page.waitForFunction(() => document.querySelector("video")?.currentTime > 0);
  fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
  await page.screenshot({ path: path.join(root, "test-results/media-generation.png"), animations: "disabled", timeout: 10000 });
  await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
  assert.equal(posts.length, 3); assert(chatRequests.every(body => !JSON.stringify(body).includes("MEDIA_FIXTURE_KEY")));
  pending = true; await run("image");
  const pendingJob = await page.evaluate(async () => (await window.xuloom.invoke("media.jobs", { session: document.querySelector('.session-item.active').getAttribute('data-session-id') })).jobs.find(row => row.phase === "submitted"));
  assert(pendingJob); await page.getByRole("textbox", { name: "任务输入", exact: true }).fill("UNSENT_MEDIA_CONTROL_DRAFT");
  const callsBeforeControl = chatRequests.length, readsBeforeControl = statusRequests; pending = false;
  await page.getByLabel('访问权限', { exact: true }).selectOption('ask');
  await page.waitForFunction(() => !document.querySelector('.permission-picker')?.disabled);
  await settings(); const jobRow = page.locator('.media-job-history details').filter({ hasText: pendingJob.id });
  await jobRow.locator('summary').click(); await jobRow.getByRole('button', { name: '查询供应商', exact: true }).click();
  await page.getByRole('dialog', { name: '工作台设置', exact: true }).waitFor({ state: 'detached' });
  await page.getByRole('button', { name: '允许一次', exact: true }).waitFor();
  assert.equal(statusRequests, readsBeforeControl, 'The direct control must wait for the same real permission approval');
  assert.equal(chatRequests.length, callsBeforeControl);
  await page.getByRole('button', { name: '允许一次', exact: true }).click();
  await page.waitForFunction(async identity => (await window.xuloom.invoke('media.jobs', { session: document.querySelector('.session-item.active').getAttribute('data-session-id') })).jobs.some(row => row.id === identity && row.phase === 'downloaded'), pendingJob.id);
  await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
  assert.equal(chatRequests.length, callsBeforeControl, 'Direct provider status controls must not request a chat-model turn');
  assert.equal(posts.length, 4); assert.equal(await page.getByRole('textbox', { name: '任务输入', exact: true }).inputValue(), 'UNSENT_MEDIA_CONTROL_DRAFT');
  await page.getByLabel('访问权限', { exact: true }).selectOption('full');
  await page.waitForFunction(() => !document.querySelector('.permission-picker')?.disabled);
  pending = true; await run('image');
  const cancelJob = await page.evaluate(async () => (await window.xuloom.invoke('media.jobs', { session: document.querySelector('.session-item.active').getAttribute('data-session-id') })).jobs.find(row => row.phase === 'submitted'));
  assert(cancelJob); const callsBeforeCancel = chatRequests.length;
  await settings(); const cancelRow = page.locator('.media-job-history details').filter({ hasText: cancelJob.id });
  await cancelRow.locator('summary').click(); await cancelRow.getByRole('button', { name: '请求取消', exact: true }).click();
  await page.waitForFunction(async identity => (await window.xuloom.invoke('media.jobs', { session: document.querySelector('.session-item.active').getAttribute('data-session-id') })).jobs.some(row => row.id === identity && row.phase === 'cancelled'), cancelJob.id);
  await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
  assert.equal(chatRequests.length, callsBeforeCancel); assert.equal(await page.getByRole('alert').count(), 0, 'Confirmed cancellation is a successful control, not a generated artifact or tool error');
  await app.close(); app = null; await launch(); await page.locator(".session-item.active").waitFor();
  await settings(); await page.getByText("此会话已保存的生成任务", { exact: true }).waitFor();
  assert.equal(await page.locator(".media-job-history details").count(), 5); assert.equal(posts.length, 5);
  assert(!(await page.locator("body").innerText()).includes("MEDIA_FIXTURE_KEY"));
  await page.getByRole("button", { name: "移除", exact: true }).click(); await page.getByRole("button", { name: "确认移除服务", exact: true }).click();
  await page.getByText("服务已移除；供应商已有任务没有被取消。", { exact: true }).waitFor(); assert.equal(posts.length, 5);
  assert.deepEqual(errors, []);
  console.log("Media E2E passed: native settings + shared masked CLI config, deferred tool discovery, one key/multiple models, actual generated files, decoded PNG/WAV/WebM playback, source navigation, direct status without a chat-model call or draft loss, restart without re-submission and connection removal");
} catch (error) { if (page) console.error((await page.locator("body").innerText().catch(() => "")).slice(-7000)); throw error; }
finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
