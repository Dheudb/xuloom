const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON, sessionKey } = require("../electron/catalog.cjs");
const { execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-files-e2e-"));
const project = path.join(fixture, "project"), other = path.join(fixture, "other"), data = path.join(fixture, "profile"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const directory of [project, other, data, config, home]) fs.mkdirSync(directory);
fs.writeFileSync(path.join(project, "legacy.md"), "legacy one\nlegacy two\n");
fs.writeFileSync(path.join(fixture, "outside.md"), "must not expose");
fs.mkdirSync(path.join(project, "nested")); fs.writeFileSync(path.join(project, "nested/tree.md"), "# Tree\nTREE_FILE_PROOF\n");
const git = (...args) => execFileSync("git", args, { cwd: other, windowsHide: true });
git("init", "-q");
git("config", "core.autocrlf", "false"); fs.writeFileSync(path.join(other, ".gitignore"), ".xuloom/\n");
fs.writeFileSync(path.join(other, "tracked.txt"), "old\nkeep\n"); fs.writeFileSync(path.join(other, "deleted.txt"), "deleted proof\n");
git("add", "."); git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline");
fs.writeFileSync(path.join(other, "tracked.txt"), "new\nkeep\nextra\n"); fs.unlinkSync(path.join(other, "deleted.txt"));
fs.writeFileSync(path.join(other, "untracked.md"), "# New\nUNTRACKED_PROOF\n");
atomicJSON(path.join(project, ".xuloom/sessions/legacy.json"), { messages: [
  { role: "user", content: "写一个旧文件" },
  { role: "assistant", content: [{ type: "tool_use", id: "old-write", name: "file_write_tool", input: { file_path: "legacy.md", content: "legacy one\nlegacy two\n" } }] },
  { role: "user", content: [{ type: "tool_result", tool_use_id: "old-write", content: "Successfully created file legacy.md. Metadata: operation: create" }] },
  { role: "assistant", content: "旧文件已写入：`legacy.md`。" },
], metadata: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [project, other], defaultDirectory: project, workspace: [{ project, resumeId: "legacy", active: true }] });
const worker = path.join(project, ".xuloom/agents/file-writer.md"); fs.mkdirSync(path.dirname(worker), { recursive: true });
fs.writeFileSync(worker, "---\nname: file-writer\ndescription: Write a file for UI testing\ntools: Write\n---\nFILE_CHILD_ROLE");
let app, page;
const errors = [];
const textOf = (message) => typeof message.content === "string" ? message.content : (message.content || []).map((block) => block.text || "").join("\n");
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", (chunk) => raw += chunk);
  req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) { res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] })); return; }
    const task = [...body.messages].reverse().find((message) => message.role === "user" && /^FILE_/.test(textOf(message)));
    const taskText = task ? textOf(task) : "";
    const after = task ? body.messages.slice(body.messages.indexOf(task) + 1) : [];
    const results = after.filter((message) => message.role === "tool");
    const child = textOf(body.messages[0]).includes("FILE_CHILD_ROLE");
    const tool = (name, args) => ({ name, arguments: args });
    let tools = [];
    if (taskText === "FILE_CREATE") {
      if (!results.length) tools = [["note.md", "# Notes\nalpha\nbeta\n"], ["a.py", "a = 1\nb = 2\n"], ["b.json", "{}\n"], ["中文 space.md", "text\n"]]
        .map(([file_path, content]) => tool("file_write_tool", { file_path, content }));
      else if (results.length === 4) tools = [tool("file_edit_tool", { file_path: "note.md", old_string: "beta", new_string: "gamma\nextra" })];
      else if (results.length === 5) tools = [tool("file_edit_tool", { file_path: "note.md", old_string: "alpha", new_string: "ALPHA" })];
    }
    if (taskText === "FILE_EDIT") {
      if (!results.length) tools = [tool("file_read_tool", { file_path: "note.md" })];
      else if (results.length === 1) tools = [tool("file_edit_tool", { file_path: "note.md", old_string: "gamma\nextra", new_string: "changed" })];
    }
    if (taskText === "FILE_DENY" && !results.length) tools = [tool("file_write_tool", { file_path: "denied.md", content: "must not exist" })];
    if (taskText.startsWith("FILE_CHILD") && !results.length) tools = [child
      ? tool("file_write_tool", { file_path: "child.txt", content: "child result\n" })
      : tool("agent_tool", { subagent_type: "file-writer", description: "子代理写文件", prompt: "FILE_CHILD_WRITE" })];
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ id: "fixture", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
    if (tools.length) {
      chunk({ role: "assistant", tool_calls: tools.map((item, index) => ({ index, id: "file_" + results.length + "_" + index, type: "function", function: { name: item.name, arguments: JSON.stringify(item.arguments) } })) }); chunk({}, "tool_calls");
    } else { chunk({ role: "assistant", content: taskText + "_DONE\n文件：`note.md`。 [带空格文件](<./中文%20space.md>)" }); chunk({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
});
async function launch() {
  const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1", USERPROFILE: home, HOME: home, XULOOM_SKILLS_PATHS: "" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on("pageerror", (error) => errors.push(error.message));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
const dialog = () => page.getByRole("region", { name: "文件预览", exact: true });
async function finished(task) {
  await page.getByText(task + "_DONE", { exact: false }).last().waitFor();
  await page.waitForFunction(() => !document.querySelector(".send-button.stop"));
}
async function send(task) { await page.getByRole("textbox", { name: "任务输入" }).fill(task); await page.getByRole("button", { name: "发送任务", exact: true }).click(); }
async function shot(filename) {
  const png = await app.evaluate(async ({ BrowserWindow }) => {
    const web = BrowserWindow.getAllWindows()[0].webContents; await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
    await new Promise((resolve) => setTimeout(resolve, 100));
    return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString("base64");
  });
  fs.mkdirSync(path.join(root, "test-results"), { recursive: true }); fs.writeFileSync(path.join(root, "test-results", filename), Buffer.from(png, "base64"));
}
async function main() {
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    atomicJSON(path.join(config, "config.json"), { model_provider: "openai", openai_model: "file-fixture", openai_api_key: "fixture-key", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch();
    // Old histories gain clickable filenames and known creation stats without any Git repository.
    await page.locator(".inline-file-link").filter({ hasText: "legacy.md" }).click();
    await dialog().getByText("legacy one", { exact: false }).waitFor(); await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
    const legacy = page.locator(".conversation .file-changes"); assert.match(await legacy.innerText(), /\+2/);
    await page.getByRole("button", { name: "在项目 project 中新建会话", exact: true }).click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    const id = await page.locator(".session-item.active").getAttribute("data-session-id");
    await page.getByLabel("访问权限", { exact: true }).selectOption("full"); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("FILE_CREATE"); await finished("FILE_CREATE");
    const first = page.locator(".conversation .file-changes").first();
    assert.match(await first.innerText(), /已修改 4 个文件/); assert.match(await first.locator("header").innerText(), /\+8/);
    assert.equal(await first.locator(".changed-file-row").count(), 3); await first.getByRole("button", { name: "再显示 1 个文件", exact: true }).click(); assert.equal(await first.locator(".changed-file-row").count(), 4);
    const note = first.getByRole("button", { name: "打开文件 note.md", exact: true }); assert.match(await note.innerText(), /\+4/); assert.match(await note.innerText(), /-0/);
    await note.focus(); await page.keyboard.press("Enter");
    assert.equal(await dialog().getByRole("tab", { name: "差异", exact: true }).getAttribute("aria-selected"), "true");
    assert.equal(await dialog().locator(".diff-added").count(), 4);
    assert.equal(await dialog().getByLabel("新行 4", { exact: true }).innerText(), "4");
    await dialog().getByRole("tab", { name: "源码", exact: true }).click(); await dialog().getByText("ALPHA", { exact: true }).waitFor(); assert((await dialog().locator(".file-source code").allTextContents()).join("\n").includes("ALPHA\ngamma\nextra"));
    await dialog().getByRole("tab", { name: "差异", exact: true }).click(); assert((await dialog().locator(".diff-added code").allTextContents()).includes("extra"));
    await shot("file-diff.png"); await page.getByRole("button", { name: "关闭文件预览", exact: true }).click();
    await page.locator(".message-body > .markdown").getByRole("button", { name: "带空格文件", exact: true }).last().click(); await dialog().getByText("text", { exact: true }).waitFor(); await page.keyboard.press("Escape");
    await send("FILE_EDIT"); await finished("FILE_EDIT");
    const second = page.locator(".conversation .file-changes").nth(1); assert.match(await second.innerText(), /\+1/); assert.match(await second.innerText(), /-2/);
    assert.match(await note.innerText(), /\+4/, "Previous turn's counts remain unchanged");
    await second.getByRole("button", { name: "打开文件 note.md", exact: true }).click();
    assert.equal(await dialog().locator(".diff-removed").count(), 2); assert.equal(await dialog().locator(".diff-added").count(), 1);
    await shot("file-red-green.png"); await page.keyboard.press("Escape");
    await note.click(); assert((await dialog().locator(".file-diff").innerText()).includes("gamma"));
    await dialog().getByRole("tab", { name: "预览", exact: true }).click(); await dialog().getByRole("heading", { name: "Notes", exact: true }).waitFor(); assert((await dialog().innerText()).includes("changed"));
    fs.writeFileSync(path.join(project, "note.md"), "# Latest\nDISK_REFRESH_PROOF\n");
    await page.getByRole("button", { name: "刷新文件内容", exact: true }).click(); await dialog().getByText("DISK_REFRESH_PROOF", { exact: true }).waitFor();
    await page.keyboard.press("Escape"); assert.equal(await dialog().count(), 0);
    await page.getByRole("button", { name: "文件", exact: true }).click();
    await page.getByRole("button", { name: "文件夹 nested", exact: true }).click();
    await page.getByRole("button", { name: "查看文件 tree.md", exact: true }).click(); await dialog().getByText("TREE_FILE_PROOF", { exact: true }).waitFor();
    await dialog().getByRole("button", { name: "文件夹 nested", exact: true }).click();
    await dialog().getByRole("button", { name: "查看文件 tree.md", exact: true }).waitFor();
    const separator = page.getByRole("separator", { name: "调整右侧面板宽度", exact: true });
    const width = Number(await separator.getAttribute("aria-valuenow")); await separator.focus(); await page.keyboard.press("ArrowRight"); assert(Number(await separator.getAttribute("aria-valuenow")) < width);
    await shot("workspace-tree.png"); await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "变更", exact: true }).click();
    assert.equal(await page.locator(".inspector .changed-file-row").count(), 4); await page.locator(".inspector").getByRole("button", { name: "打开文件 note.md", exact: true }).click();
    await dialog().getByRole("tab", { name: "预览", exact: true }).click(); await dialog().getByText("DISK_REFRESH_PROOF", { exact: true }).waitFor();
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(2100, 1000));
    async function dragTo(control, target) {
      const width = Number(await control.getAttribute("aria-valuenow")), box = await control.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2); await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + width - target, box.y + box.height / 2, { steps: 12 }); await page.mouse.up();
      await page.waitForFunction(({ label, value }) => Number(document.querySelector(`[aria-label="${label}"]`)?.getAttribute("aria-valuenow")) === value,
        { label: await control.getAttribute("aria-label"), value: target });
    }
    await dragTo(separator, 1100);
    assert(Math.abs((await page.locator(".inspector-shell").boundingBox()).width - 1100) < 2, "Outer drag must resize beyond the old 760 cap");
    const treeSeparator = dialog().getByRole("separator", { name: "调整文件目录栏宽度", exact: true });
    await dragTo(treeSeparator, 300);
    const savedLayout = () => JSON.parse(fs.readFileSync(path.join(data, "workbench.json"), "utf8")).layout;
    await page.waitForFunction(() => !document.body.classList.contains("panel-resizing"));
    // File preview and capability panels deliberately retain separate widths.
    const layoutDeadline = Date.now() + 5000;
    while ((savedLayout().filePreviewWidth !== 1100 || savedLayout().reviewTreeWidth !== 300) && Date.now() < layoutDeadline)
      await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(savedLayout().filePreviewWidth, 1100); assert.equal(savedLayout().reviewTreeWidth, 300);
    const longLine = "READABLE_LONG_LINE_" + "可读内容-with-space ".repeat(160) + "TAIL_VISIBLE";
    fs.writeFileSync(path.join(project, "note.md"), longLine + "\n" + Array.from({ length: 200 }, (_, index) => "line " + index).join("\n"));
    await dialog().getByRole("button", { name: "刷新文件内容", exact: true }).click();
    await dialog().getByRole("tab", { name: "源码", exact: true }).click();
    await dialog().locator(".source-line code").first().filter({ hasText: "TAIL_VISIBLE" }).waitFor();
    assert.equal(await dialog().getByRole("button", { name: "自动换行", exact: true }).getAttribute("aria-pressed"), "true");
    const body = dialog().locator(".file-preview-body");
    assert(await body.evaluate(element => element.scrollWidth <= element.clientWidth + 2), "Wrapped long lines must stay readable without clipping");
    await shot("file-long-lines.png");
    await body.hover(); await page.mouse.wheel(0, 600); await page.waitForFunction(() => document.querySelector(".file-preview-body").scrollTop > 100);
    await dialog().getByRole("button", { name: "自动换行", exact: true }).click();
    assert(await body.evaluate(element => element.scrollWidth > element.clientWidth + 500), "Unwrapped source must retain horizontal scrolling");
    await body.evaluate(element => { element.scrollLeft = element.scrollWidth; });
    assert(await body.evaluate(element => element.scrollLeft > 500));
    await treeSeparator.focus(); await page.keyboard.press("ArrowRight"); assert.equal(await treeSeparator.getAttribute("aria-valuenow"), "276");
    await dialog().getByRole("button", { name: "自动换行", exact: true }).click();
    fs.writeFileSync(path.join(project, "note.md"), "# Latest\nDISK_REFRESH_PROOF\n");
    await dialog().getByRole("button", { name: "刷新文件内容", exact: true }).click();
    await dialog().getByRole("tab", { name: "预览", exact: true }).click();
    await dialog().getByText("DISK_REFRESH_PROOF", { exact: true }).waitFor(); await shot("file-preview.png"); await page.keyboard.press("Escape");
    await send("FILE_CHILD"); await finished("FILE_CHILD");
    assert(fs.existsSync(path.join(project, "child.txt"))); assert.equal(await page.locator(".conversation .file-changes").last().getByRole("button", { name: "打开文件 child.txt", exact: true }).count(), 1);
    await page.getByLabel("访问权限", { exact: true }).selectOption("ask"); await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("FILE_DENY"); await page.getByRole("button", { name: "拒绝", exact: true }).click(); await finished("FILE_DENY");
    assert(!fs.existsSync(path.join(project, "denied.md"))); assert.equal(await page.locator(".conversation .file-changes").count(), 3);
    const denied = await page.evaluate(async (id) => { try { await window.xuloom.invoke("files.read", { session: id, path: "../outside.md" }); return false; } catch { return true; } }, id); assert(denied);
    await app.close(); await launch();
    assert.equal(await page.locator(".conversation .file-changes").count(), 3);
    const undoChild = page.getByRole("button", { name: "撤销最近一次写入 child.txt", exact: true });
    await undoChild.click();
    await page.getByRole("button", { name: "确认撤销", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".file-undo-confirm"));
    assert(!fs.existsSync(path.join(project, "child.txt")), "Persisted child file revision can be undone after restart");
    await page.getByRole("button", { name: "撤销最近一次写入 note.md", exact: true }).first().click();
    await page.getByRole("button", { name: "确认撤销", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "拒绝覆盖" }).waitFor();
    assert(fs.readFileSync(path.join(project, "note.md"), "utf8").includes("DISK_REFRESH_PROOF"), "Undo cannot erase external edits");
    await page.locator(".file-undo-confirm").getByRole("button", { name: "取消", exact: true }).click();
    await page.locator(".message-body > .markdown .inline-file-link").filter({ hasText: "note.md" }).first().click(); await dialog().getByText("DISK_REFRESH_PROOF", { exact: true }).waitFor(); await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "在项目 other 中新建会话", exact: true }).click(); await page.waitForFunction(() => !document.querySelector(".new-task").disabled);
    await page.getByRole("button", { name: "变更", exact: true }).click(); assert.equal(await page.locator(".inspector .changed-file-row").count(), 0);
    await page.locator(".git-changes summary").waitFor(); await page.locator(".git-changes summary").click();
    await page.waitForFunction(() => document.querySelectorAll(".git-changes .changed-file-row").length === 3);
    await page.locator(".git-changes").getByRole("button", { name: "打开文件 tracked.txt", exact: true }).click();
    await dialog().locator(".diff-removed").waitFor(); assert.equal(await dialog().locator(".diff-added").count(), 2);
    await dialog().getByRole("button", { name: "变更文件", exact: true }).click();
    await dialog().getByRole("button", { name: "打开文件 deleted.txt", exact: true }).waitFor();
    assert.equal(await dialog().locator(".changed-file-row").count(), 3);
    await shot("git-review.png"); await page.keyboard.press("Escape");
    await page.locator(".git-changes summary").waitFor(); await page.locator(".git-changes summary").click();
    await page.locator(".git-changes").getByRole("button", { name: "打开文件 deleted.txt", exact: true }).click();
    await dialog().locator(".diff-removed").waitFor(); assert.match(await dialog().locator(".diff-removed code").innerText(), /deleted proof/);
    await page.keyboard.press("Escape"); await page.locator(".git-changes summary").waitFor(); await page.locator(".git-changes summary").click();
    await page.locator(".git-changes").getByRole("button", { name: "打开文件 untracked.md", exact: true }).click();
    await dialog().getByText("UNTRACKED_PROOF", { exact: true }).waitFor(); assert.equal(await dialog().locator(".diff-added").count(), 2);
    await page.keyboard.press("Escape");
    assert.deepEqual(errors, []);
    console.log("File E2E passed: no-Git writes, net +/− stats, docked red/green numbered diff, nested workspace tree, resizing, Markdown/source/latest disk refresh, legacy and deleted-file review, child changes, denied writes, path bounds, per-turn/project isolation, restart persistence and tracked/deleted/untracked Git review");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
