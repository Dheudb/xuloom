const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-sequence-e2e-"));
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
      const task = [...body.messages].reverse().find(message => message.role === "user" && typeof message.content === "string" && /^SEQUENCE_(APP|DEFER|CLI)$/.test(message.content));
      assert(task, "Expected an owned sequence task");
      const name = task.content; counts[name] = (counts[name] || 0) + 1; counts.current = name + counts[name];
      const active = body.tools.some(tool => tool.function?.name === "tool_sequence");
      if (!active) return reply(res, null, "tool_search_tool", { query: "literal tool sequence", activate: ["tool_sequence"] });
      const after = body.messages.slice(body.messages.indexOf(task) + 1);
      if (!after.some(message => message.role === "tool" && String(message.content).includes("sequence_id:"))) {
        const directory = name === "SEQUENCE_CLI" ? project : currentWorkspace;
        const steps = [
          { id: "first", tool: "file_write_tool", arguments: { file_path: path.join(directory, name + "-first.txt"), content: "FIRST" } },
          { id: "second", tool: "file_write_tool", arguments: { file_path: path.join(directory, name + "-second.txt"), content: "SECOND" }, when: [{ step: "first", path: ["ok"], equals: name !== "SEQUENCE_DEFER" }] },
        ];
        return reply(res, null, "tool_sequence", { steps });
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
    await send("SEQUENCE_APP");
    assert.equal(counts.SEQUENCE_APP, 3, "Discovery + one sequence + final response, no model call between child tools");
    for (const file of ["first", "second"]) assert.equal(fs.readFileSync(path.join(currentWorkspace, `SEQUENCE_APP-${file}.txt`), "utf8"), file === "first" ? "FIRST" : "SECOND");
    const trace = page.locator(".turn-process").first(); await trace.locator(":scope > summary").click();
    assert.equal(await trace.locator('[data-tool-kind="sequence"]').count(), 1);
    assert.equal(await trace.locator('[data-tool-kind="edit"]').count(), 2);
    await send("SEQUENCE_DEFER"); assert.equal(counts.SEQUENCE_DEFER, 2);
    assert(!fs.existsSync(path.join(currentWorkspace, "SEQUENCE_DEFER-second.txt")));
    await app.close(); app = null; const before = { ...counts }; await launch();
    await page.getByText("SEQUENCE_APP_PASS", { exact: true }).waitFor(); assert.deepEqual(counts, before, "Restoring UI must not execute a saved sequence");
    await app.close(); app = null;
    await new Promise((resolve, reject) => {
      const env = environment(); if (!process.env.XULOOM_TEST_EXECUTABLE) env.PYTHONPATH = path.resolve(root, "..");
      const child = spawn(python, [...(process.env.XULOOM_TEST_EXECUTABLE ? ["-I", "-B"] : []), "-m", "xuloom", "exec", "SEQUENCE_CLI", "--cwd", project, "--permission", "full", "--json"], { env, windowsHide: true });
      let stdout = "", stderr = ""; const timer = setTimeout(() => { child.kill(); reject(Error("Owned CLI timed out")); }, 60000);
      child.stdout.on("data", value => stdout += value); child.stderr.on("data", value => stderr += value); child.on("error", reject);
      child.on("close", code => { clearTimeout(timer); try { assert.equal(code, 0, stderr); const rows = stdout.trim().split(/\r?\n/).map(JSON.parse); assert.equal(rows.at(-1).result.text, "SEQUENCE_CLI_PASS"); resolve(); } catch (error) { reject(error); } });
    });
    assert.equal(counts.SEQUENCE_CLI, 3); assert.equal(fs.readFileSync(path.join(project, "SEQUENCE_CLI-second.txt"), "utf8"), "SECOND");
    assert.deepEqual(failures, []);
    console.log("PASS: hidden App + formal CLI, actual native file writes, guarded deferral, no intermediate actor calls, child activity/icons, restart without replay; loopback model only");
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
