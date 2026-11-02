const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-native-continuation-e2e-"));
const data = path.join(fixture, "data"), config = path.join(fixture, "config"), home = path.join(fixture, "home"), project = path.join(fixture, "project");
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), "resources/python/python.exe") : process.env.XULOOM_PYTHON || "python";
fs.writeFileSync(path.join(data, "workbench.json"), JSON.stringify({ projects: [], defaultDirectory: project, python }));
const failures = [], counts = {};
let app, page, currentWorkspace;
function reply(res, text, name, input) {
  res.writeHead(200, { "Content-Type": "text/event-stream" });
  const chunk = (delta, finish_reason = null) => res.write("data: " + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + "\n\n");
  if (name) { chunk({ tool_calls: [{ index: 0, id: "owned-" + counts.current, type: "function", function: { name, arguments: JSON.stringify(input) } }] }); chunk({}, "tool_calls"); }
  else { chunk({ content: text }); chunk({}, "stop"); }
  res.end('data: [DONE]\n\n');
}
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", value => raw += value);
  req.on("end", () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ choices: [{ message: { role: "assistant", content: "memory" } }] }));
      const task = [...body.messages].reverse().find(message => message.role === "user" && typeof message.content === "string" && /^NATIVE_(APP|DEFER|CLI)$/.test(message.content));
      assert(task, "Expected an owned sequence task");
      const name = task.content; counts[name] = (counts[name] || 0) + 1; counts.current = name + counts[name];
      assert(body.tools.find(tool => tool.function?.name === "file_write_tool").function.parameters.properties.__xuloom_continuation);
      const after = body.messages.slice(body.messages.indexOf(task) + 1);
      if (!after.some(message => message.role === "tool")) {
        const directory = name === "NATIVE_CLI" ? project : currentWorkspace;
        return reply(res, null, "file_write_tool", {file_path: path.join(directory, name + "-first.txt"), content: "FIRST",
          __xuloom_continuation: {nodes: [{id: "second", tool: "file_write_tool",
            arguments: {file_path: path.join(directory, name + "-second.txt"), content: "SECOND"},
            when: [{step: "root", path: ["ok"], equals: name !== "NATIVE_DEFER"}]}]}});
      }
      return reply(res, name + "_PASS");
    } catch (error) { failures.push(error.stack); res.writeHead(500).end("Owned fixture failed"); }
  });
});
function environment() {
  const env = { ...process.env, XULOOM_TEST_HEADLESS: "1", XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: "", HOME: home, USERPROFILE: home, XULOOM_NETWORK_MODE: "direct" };
  for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER", "PYTHONHOME", "PYTHONPATH"]) delete env[key];
  return env;
}
async function launch() {
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env: environment(), timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(45000);
  page.on("pageerror", error => failures.push(error.stack));
  await page.waitForFunction(() => document.querySelector(".new-task")?.disabled === false);
}
async function send(text) {
  await page.getByRole("textbox", { name: "任务输入" }).fill(text);
  await page.getByRole("button", { name: "发送任务", exact: true }).click();
  await page.getByText(text + "_PASS", { exact: true }).waitFor();
}
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  fs.writeFileSync(path.join(config, "config.json"), JSON.stringify({ model_provider: "openai", openai_model: "fixture", openai_api_key: "owned-no-provider", openai_base_url: `http://127.0.0.1:${server.address().port}/v1` }));
  try {
    await launch(); await page.locator(".new-task").click(); await page.locator(".session-item.active").waitFor();
    currentWorkspace = JSON.parse(fs.readFileSync(path.join(data, "workbench.json"))).activeConversation.project;
    await page.getByLabel("访问权限", { exact: true }).selectOption("full");
    await page.waitForFunction(() => !document.querySelector(".permission-picker").disabled);
    await send("NATIVE_APP");
    assert.equal(counts.NATIVE_APP, 2, "Root with metadata + final response, no intermediate actor request");
    for (const file of ["first", "second"]) assert.equal(fs.readFileSync(path.join(currentWorkspace, `NATIVE_APP-${file}.txt`), "utf8"), file === "first" ? "FIRST" : "SECOND");
    const trace = page.locator(".turn-process").first(); await trace.locator(":scope > summary").click();
    await trace.getByText(/按已声明计划执行/).waitFor();
    assert.equal(await trace.locator('[data-tool-kind="edit"]').count(), 2);
    await send("NATIVE_DEFER"); assert.equal(counts.NATIVE_DEFER, 2);
    assert(!fs.existsSync(path.join(currentWorkspace, "NATIVE_DEFER-second.txt")));
    await app.close(); app = null; const before = { ...counts }; await launch();
    await page.getByText("NATIVE_APP_PASS", { exact: true }).waitFor(); assert.deepEqual(counts, before, "Restoring UI must not execute a saved sequence");
    await app.close(); app = null;
    await new Promise((resolve, reject) => {
      const env = environment(); if (!process.env.XULOOM_TEST_EXECUTABLE) env.PYTHONPATH = path.resolve(root, "..");
      const child = spawn(python, [...(process.env.XULOOM_TEST_EXECUTABLE ? ["-I", "-B"] : []), "-m", "xuloom", "exec", "NATIVE_CLI", "--cwd", project, "--permission", "full", "--json"], { env, windowsHide: true });
      let stdout = "", stderr = ""; const timer = setTimeout(() => { child.kill(); reject(Error("Owned CLI timed out")); }, 60000);
      child.stdout.on("data", value => stdout += value); child.stderr.on("data", value => stderr += value); child.on("error", reject);
      child.on("close", code => { clearTimeout(timer); try { assert.equal(code, 0, stderr); const rows = stdout.trim().split(/\r?\n/).map(JSON.parse); assert.equal(rows.at(-1).result.text, "NATIVE_CLI_PASS"); assert(rows.some(row => row.reason === "continuation_substitution")); resolve(); } catch (error) { reject(error); } });
    });
    assert.equal(counts.NATIVE_CLI, 2); assert.equal(fs.readFileSync(path.join(project, "NATIVE_CLI-second.txt"), "utf8"), "SECOND");
    assert.deepEqual(failures, []);
    console.log("PASS: native request boundary in hidden App and formal CLI, two real file writes, false branch skipped, two actor requests, certificates, visible explanation, restart without replay; loopback model only");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
