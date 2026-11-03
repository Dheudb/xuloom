const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict"), { execFileSync } = require("node:child_process");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-attachments-e2e-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const dir of [data, config, home]) fs.mkdirSync(dir);
const image = path.join(fixture, "示例 图片.png"), pdf = path.join(fixture, "研究论文.pdf");
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe") : path.join(root, "../.venv-mcp/Scripts/python.exe");
execFileSync(python, ["-c", "from PIL import Image; import fitz,sys; Image.new('RGB',(100,60),'#bd654d').save(sys.argv[1]); d=fitz.open(); p=d.new_page(); p.insert_text((50,50),'PDF_FILE_PROOF'); d.save(sys.argv[2]); d.close()", image, pdf], { windowsHide: true });
const png = fs.readFileSync(image), failures = [], requests = [];
let app, page;
atomicJSON(path.join(data, "workbench.json"), { projects: [], activeConversation: null });
const textOf = (m) => typeof m?.content === "string" ? m.content : (m?.content || []).map((b) => b.text || "").join("\n");
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk); req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
    requests.push(body);
    const user = body.messages.find((m) => m.role === "user" && Array.isArray(m.content) && m.content.some((b) => b.type === "image_url"));
    if (!user) failures.push("Missing image bytes in actual model request");
    const pixels = user?.content.find((b) => b.type === "image_url")?.image_url.url || "";
    if (!pixels.startsWith("data:image/jpeg;base64,")) failures.push("Image is not sent as visual data");
    const text = textOf(user), refs = text.split("\n").filter((line) => line.startsWith("{")).map((line) => { try { return JSON.parse(line); } catch { return {}; } });
    const file = refs.find((item) => item.name?.endsWith(".pdf"));
    const result = body.messages.find((m) => m.role === "tool");
    if (file && !path.resolve(file.path).toLowerCase().startsWith(path.resolve(data).toLowerCase() + path.sep)) failures.push("File must use a session-owned copy");
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (file && !result) {
      chunk({ role: "assistant", tool_calls: [{ index: 0, id: "read-pdf", type: "function", function: { name: "file_read_tool", arguments: JSON.stringify({ file_path: file.path, pages: "1" }) } }] }); chunk({}, "tool_calls");
    } else {
      if (file && !textOf(result).includes("PDF_FILE_PROOF")) failures.push("PDF contents did not reach the model");
      chunk({ role: "assistant", content: file ? "ATTACHMENTS_VERIFIED" : "IMAGE_ONLY_VERIFIED" }); chunk({}, "stop");
    }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, HOME: home, USERPROFILE: home, XULOOM_TEST_HEADLESS: "1" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(90000); page.on("pageerror", (error) => failures.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
const draft = () => page.getByLabel("待发送附件", { exact: true });
async function pasteImage() {
  await page.getByRole("textbox", { name: "任务输入", exact: true }).evaluate((element, bytes) => {
    const transfer = new DataTransfer(); transfer.items.add(new File([Uint8Array.from(bytes)], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }));
  }, [...png]);
}
async function screenshot() {
  const encoded = await app.evaluate(async ({ BrowserWindow }) => {
    const web = BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
    await new Promise((resolve) => setTimeout(resolve, 100));
    return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
  });
  fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results/attachments.png"), Buffer.from(encoded, "base64"));
}
(async () => {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "vision-fixture", openai_api_key: "fixture", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch();
    // Native disk-backed File objects exercise Electron webUtils, rather than synthetic paths.
    await page.evaluate(() => { const picker = document.createElement("input"); picker.type = "file"; picker.multiple = true; picker.id = "native-drop-fixture"; picker.hidden = true; document.body.appendChild(picker); });
    await page.locator("#native-drop-fixture").setInputFiles([image, pdf]);
    await page.evaluate(() => {
      const transfer = new DataTransfer(); for (const file of document.querySelector("#native-drop-fixture").files) transfer.items.add(file);
      const area = document.querySelector(".workbench");
      area.dispatchEvent(new DragEvent("dragenter", { dataTransfer: transfer, bubbles: true, cancelable: true }));
      area.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true }));
    });
    await draft().getByRole("button", { name: "移除附件 研究论文.pdf", exact: true }).waitFor();
    assert.equal(await draft().locator(".attachment-chip").count(), 2);
    await pasteImage(); await page.waitForFunction(() => document.querySelectorAll(".attachment-draft .attachment-chip").length === 3);
    await draft().getByRole("button", { name: /移除附件 粘贴图片/ }).click();
    await page.getByLabel("选择附件文件", { exact: true }).setInputFiles(image);
    await page.waitForFunction(() => document.querySelectorAll(".attachment-draft .attachment-chip").length === 3);
    await draft().getByRole("button", { name: "移除附件 示例 图片.png", exact: true }).last().click();
    await page.getByRole("textbox", { name: "任务输入", exact: true }).fill("解释图片和论文");
    await screenshot();
    // Deleting the originals must not lose attached files or image pixels.
    fs.unlinkSync(image); fs.unlinkSync(pdf);
    await page.waitForFunction(() => document.querySelector(".session-select small")?.textContent === "有草稿");
    await app.close(); await launch();
    assert.equal(await draft().locator(".attachment-chip").count(), 2);
    await draft().getByRole("button", { name: "查看附件 示例 图片.png", exact: true }).click();
    await page.getByRole("dialog", { name: "图片附件预览", exact: true }).waitFor();
    await page.getByRole("button", { name: "关闭图片预览", exact: true }).click();
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.getByText("ATTACHMENTS_VERIFIED", { exact: true }).waitFor(); await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(await page.getByLabel("消息附件", { exact: true }).locator(".attachment-chip").count(), 2);
    assert.equal(await page.locator(".message.user .markdown").innerText(), "解释图片和论文");
    await app.close(); await launch();
    assert.equal(await page.getByLabel("消息附件", { exact: true }).locator(".attachment-chip").count(), 2);
    await page.locator(".new-task").click();
    await page.getByRole("heading", { name: "开始一个新会话。", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector(".composer textarea")?.disabled === false);
    await pasteImage(); await draft().locator(".attachment-chip").waitFor();
    assert(await page.getByRole("button", { name: "发送任务", exact: true }).isEnabled());
    await page.getByRole("button", { name: "发送任务", exact: true }).click(); await page.getByText("IMAGE_ONLY_VERIFIED", { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await page.locator('.model-options > summary').click();
    await page.getByLabel("模型输入能力", { exact: true }).selectOption("text");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "工作台设置" }).waitFor({ state: "hidden" });
    const beforeRejected = requests.length;
    await pasteImage(); await draft().locator(".attachment-chip").waitFor();
    await page.getByRole("button", { name: "发送任务", exact: true }).click();
    await page.locator(".session-error").filter({ hasText: "不支持 image" }).waitFor();
    await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
    assert.equal(requests.length, beforeRejected, "Known text-only model must not receive image payloads or image history");
    assert.deepEqual(failures, []); assert(requests.length >= 3);
    console.log("Attachments E2E passed: real disk File drop, clipboard image bytes, picker, remove, automatic independent session, persistent draft/history, image preview, deleted originals, PDF read and actual visual API payload, image-only send");
  } catch (error) {
    if (page) console.error("Attachment UI errors:", await page.locator(".session-error,.global-notice").allTextContents().catch(() => []));
    throw error;
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
