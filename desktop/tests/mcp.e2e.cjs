const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const python = process.env.XULOOM_TEST_EXECUTABLE
  ? path.join(
      path.dirname(process.env.XULOOM_TEST_EXECUTABLE),
      "resources/python/python.exe",
    )
  : process.env.XULOOM_PYTHON || "python";
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-mcp-e2e-"));
const project = path.join(fixture, "mcp-project");
const data = path.join(fixture, "app-data");
const config = path.join(fixture, "config");
const home = path.join(fixture, "home");
for (const dir of [project, data, config, home])
  fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(project, "README.md"), "# MCP test project\n");
fs.writeFileSync(
  path.join(data, "workbench.json"),
  JSON.stringify({ projects: [project], python }),
);
const modelRequests = [];
const api = http.createServer((request, response) => {
  let raw = "";
  request.on("data", (chunk) => {
    raw += chunk;
  });
  request.on("end", () => {
    const body = JSON.parse(raw);
    modelRequests.push(body);
    const last = body.messages?.at(-1);
    const active = body.tools?.some((tool) => tool.function.name === "offline_search_search");
    const discover = !active && last?.role === "user" && JSON.stringify(last.content).includes("MCP 搜索验收");
    const call = active && (last?.role === "user" || last?.tool_call_id === "fixture-discover");
    if (!body.stream) {
      response.writeHead(200, { "Content-Type": "application/json" }).end(
        JSON.stringify({
          id: "fixture",
          object: "chat.completion",
          model: "mcp-test",
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "OK" },
              finish_reason: "stop",
            },
          ],
        }),
      );
      return;
    }
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    });
    function chunk(delta, finish_reason = null) {
      response.write(
        "data: " +
          JSON.stringify({
            id: "fixture",
            object: "chat.completion.chunk",
            model: "mcp-test",
            choices: [{ index: 0, delta, finish_reason }],
          }) +
          "\n\n",
      );
    }
    if (discover || call) {
      chunk({
        role: "assistant",
        tool_calls: [
          {
            index: 0,
            id: discover ? "fixture-discover" : "fixture-search",
            type: "function",
            function: {
              name: discover ? "tool_search_tool" : "offline_search_search",
              arguments: JSON.stringify(discover
                ? { query: "offline search", activate: ["offline_search_search"] }
                : { query: "desktop-search" }),
            },
          },
        ],
      });
      chunk({}, "tool_calls");
    } else {
      chunk({ role: "assistant", content: "MCP_END_TO_END_OK" });
      chunk({}, "stop");
    }
    response.end("data: [DONE]\n\n");
  });
});

async function main() {
  const service = spawn(
    process.execPath,
    [path.join(root, "../tests/fixtures/mcp-server.cjs"), "--http"],
    { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
  );
  const port = await new Promise((resolve, reject) => {
    let text = "";
    service.on("error", reject);
    service.stdout.on("data", (chunk) => {
      text += chunk;
      if (text.includes("\n")) resolve(JSON.parse(text.trim()).port);
    });
  });
  await new Promise((resolve) => api.listen(0, "127.0.0.1", resolve));
  const env = {
    ...process.env,
    XULOOM_TEST_HEADLESS: "1",
    XULOOM_USER_DATA: data,
    XULOOM_DATA_DIR: data,
    XULOOM_CONFIG_DIR: config,
    XULOOM_SKILLS_PATHS: "",
    USERPROFILE: home,
    HOME: home,
    XULOOM_MODEL_PROVIDER: "openai",
    OPENAI_MODEL: "mcp-test",
    OPENAI_API_KEY: "local-fixture-only",
    OPENAI_BASE_URL: `http://127.0.0.1:${api.address().port}/v1`,
    NO_PROXY: "127.0.0.1,localhost",
  };
  if (process.env.XULOOM_TEST_EXECUTABLE) {
    env.XULOOM_PYTHON = "Z:\\missing-system-python\\python.exe";
    env.PYTHONHOME = "Z:\\missing-system-python";
    env.PYTHONPATH = "Z:\\missing-site-packages";
  }
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.XULOOM_DEV_URL;
  const app = await electron.launch({
    executablePath: process.env.XULOOM_TEST_EXECUTABLE,
    args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root],
    env,
    timeout: 30000,
  });
  const page = await app.firstWindow();
  page.setDefaultTimeout(45000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const output = path.join(root, "test-results");
  fs.mkdirSync(output, { recursive: true });
  async function capture(name) {
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        ),
    );
    const png = await app.evaluate(async ({ BrowserWindow }) =>
      (
        await BrowserWindow.getAllWindows()[0].webContents.capturePage(
          undefined,
          { stayHidden: true, stayAwake: true },
        )
      )
        .toPNG()
        .toString("base64"),
    );
    fs.writeFileSync(path.join(output, name), Buffer.from(png, "base64"));
  }
  try {
    await page.locator(".project-item").click();
    await page.getByRole("heading", { name: "今天想完成什么？" }).waitFor();
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    assert.equal(
      await page.getByText("选择解释器", { exact: true }).count(),
      0,
    );
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page.getByRole("button", { name: "MCP 市场", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "MCP 市场" });
    await dialog.getByText("Exa Search", { exact: true }).waitFor();
    // A malformed mounted extension and a hand-edited invalid server must be
    // visible and repairable without preventing the rest of the settings UI.
    const ownedSkill = path.join(project, ".xuloom", "skills", "owned-config-check");
    fs.mkdirSync(ownedSkill, { recursive: true });
    fs.writeFileSync(path.join(ownedSkill, "SKILL.md"), "---\nname: owned-config-check\ndescription: Local configuration error fixture\n---\nOwned fixture.\n");
    fs.writeFileSync(path.join(ownedSkill, "mcp_servers.json"), "{not-json");
    const settingsPath = path.join(project, ".xuloom", "mcp_servers.json");
    fs.writeFileSync(settingsPath, JSON.stringify({ mcpServers: { invalid_config: { type: [], enabled: "false", command: "owned-never-started", headers: { Authorization: "owned-do-not-display" } } } }));
    await dialog.getByRole("button", { name: /我的服务/ }).click();
    await dialog.getByRole("button", { name: "刷新连接", exact: true }).click();
    await dialog.getByText("部分 Skill 的 MCP 配置未加载，其他服务可继续使用。", { exact: true }).waitFor();
    await dialog.getByText("配置需修复", { exact: true }).waitFor();
    assert(!(await dialog.innerText()).includes("owned-do-not-display"));
    await capture("mcp-invalid-configuration.png");
    const invalidCard = dialog.locator(".mcp-card").filter({ hasText: "invalid_config" });
    await invalidCard.getByRole("button", { name: "配置", exact: true }).click();
    await dialog.getByLabel("MCP 连接方式", { exact: true }).selectOption("stdio");
    await dialog.getByLabel("MCP 启动程序", { exact: true }).fill("owned-not-launched");
    await dialog.getByRole("button", { name: "保存配置", exact: true }).click();
    await dialog.getByText("已停用", { exact: true }).waitFor();
    await invalidCard.getByRole("button", { name: "移除", exact: true }).click();
    await dialog.getByRole("button", { name: "确认移除", exact: true }).click();
    // Remove only the fixture created above, not an external/user Skill.
    fs.unlinkSync(path.join(ownedSkill, "mcp_servers.json"));
    await dialog.getByRole("button", { name: "关闭 MCP 市场", exact: true }).click();
    await page.getByRole("button", { name: "MCP 市场", exact: true }).click();
    await dialog.getByRole("button", { name: /我的服务/ }).click();
    assert.equal(await dialog.getByText("部分 Skill 的 MCP 配置未加载，其他服务可继续使用。", { exact: true }).count(), 0);
    await dialog.getByRole("button", { name: "浏览市场", exact: true }).click();
    await capture("mcp-market.png");
    // Installing a search service stores config without starting it or requiring a model.
    await dialog
      .locator(".mcp-card")
      .filter({ hasText: "Brave Search" })
      .getByRole("button", { name: "安装", exact: true })
      .click();
    await dialog
      .getByLabel("BRAVE_API_KEY", { exact: true })
      .fill("credential-redaction-proof");
    await dialog.getByRole("button", { name: "安装并保存配置" }).click();
    await dialog.getByText("已停用", { exact: true }).waitFor();
    assert(
      !(await dialog
        .innerText()
        .then((text) => text.includes("credential-redaction-proof"))),
    );
    const stored = JSON.parse(
      fs.readFileSync(path.join(project, ".xuloom", "mcp_servers.json"), "utf8"),
    );
    assert.equal(stored.mcpServers["brave-search"].enabled, false);
    await dialog.getByRole("button", { name: "移除", exact: true }).click();
    await dialog.getByRole("button", { name: "确认移除", exact: true }).click();
    // Custom HTTP server: edit, credential preservation, handshake, and discovery.
    await dialog.getByRole("button", { name: "自定义服务" }).click();
    await dialog.getByLabel("MCP 服务标识").fill("offline_search");
    await dialog
      .getByLabel("MCP 服务地址")
      .fill(`http://127.0.0.1:${port}/mcp`);
    await dialog.getByRole("button", { name: "添加字段" }).click();
    await dialog
      .getByLabel("配置名称 1", { exact: true })
      .fill("Authorization");
    await dialog
      .getByLabel("配置值 1", { exact: true })
      .fill("Bearer fixture-secret");
    await dialog.getByRole("button", { name: "保存配置", exact: true }).click();
    await dialog.getByRole("button", { name: "测试连接", exact: true }).click();
    await dialog
      .getByText("连接成功，发现 1 个工具。", { exact: true })
      .waitFor();
    await dialog.getByRole("button", { name: "配置", exact: true }).click();
    assert.equal(
      await dialog.getByLabel("配置值 1", { exact: true }).inputValue(),
      "",
    );
    await dialog.getByRole("button", { name: "保存配置", exact: true }).click();
    const shared = execFileSync(
      python,
      [
        ...(process.env.XULOOM_TEST_EXECUTABLE ? ["-I"] : []),
        "-c",
        "import sys,json; from xuloom.protocols.mcp_config import MCPConfigStore; print(json.dumps(MCPConfigStore(sys.argv[1]).snapshot()))",
        path.join(project, ".xuloom", "mcp_servers.json"),
      ],
      { cwd: path.resolve(root, ".."), env, windowsHide: true, encoding: "utf8" },
    );
    assert(!shared.includes("fixture-secret"));
    assert(shared.includes("offline_search"));
    const storedText = fs.readFileSync(path.join(project, ".xuloom", "mcp_servers.json"), "utf8");
    const storedAuth = JSON.parse(storedText).mcpServers.offline_search.headers.Authorization;
    if (process.platform === "win32") {
      assert.equal(storedAuth.$xuloom_secret, "dpapi-v1");
      assert(!storedText.includes("fixture-secret"));
    } else assert.equal(storedAuth, "Bearer fixture-secret");
    const authProof = execFileSync(python, [
      ...(process.env.XULOOM_TEST_EXECUTABLE ? ["-I"] : []), "-c",
      "import asyncio,sys\nfrom xuloom.protocols.mcp_bridge import MCPBridge\nasync def run():\n bridge=MCPBridge(sys.argv[1])\n try:\n  await bridge.load_and_connect(only='offline_search',include_disabled=True)\n  result=await bridge.sessions['offline_search'].call_tool('search',{'query':'__header__'})\n  assert result.content[0].text=='Bearer fixture-secret'\n finally:\n  await bridge.cleanup()\nasyncio.run(run())\nprint('OWNED_MCP_AUTH_OK')",
      path.join(project, ".xuloom", "mcp_servers.json"),
    ], { env, encoding: "utf8", windowsHide: true });
    assert(authProof.includes("OWNED_MCP_AUTH_OK"));
    await dialog.getByRole("button", { name: "启用", exact: true }).click();
    await page.getByRole("button", { name: "关闭 MCP 市场" }).click();
    await page.getByRole("textbox", { name: "任务输入" }).fill("MCP 搜索验收");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page.getByRole("button", { name: "允许一次", exact: true }).click();
    await page.getByText("MCP_END_TO_END_OK", { exact: true }).waitFor();
    await page.getByRole("button", { name: "发送任务" }).waitFor();
    const initial = modelRequests.find((body) => body.stream);
    assert(!initial.tools.some((tool) => tool.function.name === "offline_search_search"));
    assert(modelRequests.some((body) => body.tools?.some((tool) => tool.function.name === "offline_search_search")));
    assert(
      modelRequests.some((body) =>
        body.messages.some(
          (m) =>
            m.role === "tool" &&
            String(m.content).includes("SEARCH_FIXTURE_PROOF"),
        ),
      ),
    );
    assert(!JSON.stringify(modelRequests).includes("fixture-secret"));
    await page.getByRole("button", { name: "MCP 市场", exact: true }).click();
    await dialog.getByRole("button", { name: /我的服务/ }).click();
    await dialog.getByText("已连接", { exact: true }).waitFor();
    await dialog.getByRole("button", { name: "停用", exact: true }).click();
    await page.getByRole("button", { name: "关闭 MCP 市场" }).click();
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("停用 MCP 后继续对话");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page.getByText("MCP_END_TO_END_OK", { exact: true }).nth(1).waitFor();
    const last = [...modelRequests].reverse().find((body) => body.stream);
    assert(
      !(last.tools || []).some(
        (tool) => tool.function.name === "offline_search_search",
      ),
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: hidden Electron MCP market install/remove, custom HTTP config, real MCP test, credential preservation/redaction, shared CLI config, on-demand discovery/schema activation, model tool call with permission, disable/reload",
    );
  } catch (error) {
    await capture("mcp-failure.png");
    console.error((await page.locator("body").innerText()).slice(-10000));
    console.error("Fixture retained at " + fixture);
    throw error;
  } finally {
    await app.close();
    service.kill();
    api.close();
  }
}
main().catch((error) => {
  console.error(error);
  api.close();
  process.exitCode = 1;
});
