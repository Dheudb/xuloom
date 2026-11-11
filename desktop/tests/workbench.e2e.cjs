const { historyRoot } = require("../electron/history-storage.cjs");
const { _electron: electron } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-desktop-e2e-"));
const project = path.join(fixture, "sample-project");
const secondProject = path.join(fixture, "another-project");
const data = path.join(fixture, "app-data");
const config = path.join(fixture, "config");
for (const directory of [project, secondProject, data, config])
  fs.mkdirSync(directory, { recursive: true });
const skill = path.join(project, ".xuloom/skills/project-review");
fs.mkdirSync(path.join(skill, "references"), { recursive: true });
fs.writeFileSync(
  path.join(skill, "SKILL.md"),
  "---\nname: project-review\ndescription: Review project structure and implementation\n---\nDESKTOP_SKILL_PROOF: Read references/checklist.md before reviewing.",
);
fs.writeFileSync(
  path.join(skill, "references/checklist.md"),
  "Verify project boundaries and tests.",
);
const agentDir = path.join(project, ".xuloom/agents");
fs.mkdirSync(agentDir, { recursive: true });
fs.writeFileSync(
  path.join(agentDir, "reviewer.md"),
  "---\nname: reviewer\ndescription: Independent project reviewer\ntools: Read, Grep, Glob, Skill\nskills: [project-review]\n---\nDESKTOP_CHILD_PROOF: review independently and report findings.",
);
fs.writeFileSync(path.join(project, "README.md"), "# Sample project\n");
execFileSync("git", ["init", "-q", project], { windowsHide: true });
execFileSync("git", ["add", "README.md"], { cwd: project, windowsHide: true });
execFileSync(
  "git",
  [
    "-c",
    "user.name=Desktop Test",
    "-c",
    "user.email=test@example.invalid",
    "commit",
    "-qm",
    "fixture",
  ],
  { cwd: project, windowsHide: true },
);
fs.writeFileSync(
  path.join(data, "workbench.json"),
  JSON.stringify({
    projects: [project, secondProject],
    python: process.env.XULOOM_PYTHON || "python",
  }),
);
const received = [];
const requestedPaths = [];
const messageText = (message) =>
  typeof message.content === "string"
    ? message.content
    : (message.content || []).map((block) => block.text || "").join("\n");
const isChildRequest = (body) =>
  body.messages.some(
    (message) =>
      message.role === "system" &&
      messageText(message).includes(
        "DESKTOP_CHILD_PROOF: review independently",
      ),
  );
const server = http.createServer((request, response) => {
  requestedPaths.push(request.url);
  let raw = "";
  request.on("data", (chunk) => {
    raw += chunk;
  });
  request.on("end", () => {
    const body = JSON.parse(raw);
    received.push(body);
    const users = (body.messages || []).filter(
      (message) => message.role === "user",
    );
    const taskMessage = [...users]
      .reverse()
      .find((message) =>
        /^(验收文件|验收子代理|DESKTOP_CHILD_PROOF)/.test(messageText(message)),
      );
    const latest = taskMessage
      ? messageText(taskMessage)
      : messageText(users.at(-1) || {});
    const last = body.messages.at(-1);
    const child = isChildRequest(body);
    let tools = null;
    let text = "桌面验收完成：项目已挂载，技能已加载，模型流式连接正常。";
    if (child) text = "子代理验收完成：独立上下文与技能指令已收到。";
    else if (
      latest.startsWith("验收文件") &&
      !body.messages
        .slice(body.messages.indexOf(taskMessage) + 1)
        .some((message) => message.role === "tool")
    )
      tools = [
        {
          name: "file_write_tool",
          arguments: {
            file_path: path.join(project, "desktop-proof.txt"),
            content: "Created only after desktop approval.",
          },
        },
      ];
    else if (
      latest.startsWith("验收子代理") &&
      !body.messages
        .slice(body.messages.indexOf(taskMessage) + 1)
        .some((message) => message.role === "tool")
    )
      tools = [
        {
          name: "agent_tool",
          arguments: {
            description: "Review project",
            prompt: "DESKTOP_CHILD_PROOF Review the project independently.",
            subagent_type: "reviewer",
            run_in_background: true,
          },
        },
        {
          name: "agent_tool",
          arguments: {
            description: "Second review",
            prompt: "DESKTOP_CHILD_PROOF Perform a second independent review.",
            subagent_type: "reviewer",
            run_in_background: true,
          },
        },
      ];
    if (!body.stream) {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(
        JSON.stringify({
          id: "test",
          object: "chat.completion",
          model: body.model,
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: text },
              finish_reason: "stop",
            },
          ],
          usage: {
            prompt_tokens: 100,
            completion_tokens: 20,
            total_tokens: 120,
          },
        }),
      );
      return;
    }
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    });
    const chunk = (delta, finish_reason = null) =>
      response.write(
        "data: " +
          JSON.stringify({
            id: "test",
            object: "chat.completion.chunk",
            created: 1,
            model: body.model,
            choices: [{ index: 0, delta, finish_reason }],
          }) +
          "\n\n",
      );
    if (tools) {
      chunk({
        role: "assistant",
        tool_calls: tools.map((tool, index) => ({
          index,
          id: "call-" + index + "-" + received.length,
          type: "function",
          function: {
            name: tool.name,
            arguments: JSON.stringify(tool.arguments),
          },
        })),
      });
      chunk({}, "tool_calls");
    } else {
      chunk({ role: "assistant", content: text.slice(0, 12) });
      chunk({ content: text.slice(12) });
      chunk({}, "stop");
    }
    response.end("data: [DONE]\n\n");
  });
});

async function main() {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const baseUrl = "http://127.0.0.1:" + server.address().port + "/v1";
  fs.writeFileSync(
    path.join(config, "config.json"),
    JSON.stringify({
      model_provider: "openai",
      openai_model: "desktop-test",
      openai_base_url: baseUrl,
      openai_api_key: "local-test-key",
    }),
  );
  const env = {
    ...process.env,
    USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"),
    XULOOM_TEST_HEADLESS: "1",
    XULOOM_USER_DATA: data,
    XULOOM_CONFIG_DIR: config,
    XULOOM_MODEL_PROVIDER: "openai",
    OPENAI_API_KEY: "local-test-key",
    OPENAI_BASE_URL: baseUrl,
    OPENAI_MODEL: "desktop-test",
    // Reproduce a broken inherited proxy: direct model mode must bypass it.
    HTTP_PROXY: "http://127.0.0.1:1",
    HTTPS_PROXY: "http://127.0.0.1:1",
    NO_PROXY: "",
  };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.XULOOM_DEV_URL;
  const launchOptions = {
    executablePath: process.env.XULOOM_TEST_EXECUTABLE,
    args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root],
    env,
    timeout: 30000,
  };
  let app = await electron.launch(launchOptions);
  let page = await app.firstWindow();
  await page.emulateMedia({ reducedMotion: "reduce" });
  const events = [];
  await page.exposeFunction("recordWorkbenchEvent", (event) => {
    events.push(event);
    fs.appendFileSync(
      path.join(root, "test-results/events.jsonl"),
      JSON.stringify(event) + "\n",
    );
  });
  await page.evaluate(() =>
    window.xuloom.onEvent((event) => window.recordWorkbenchEvent(event)),
  );
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.setDefaultTimeout(45000);
  async function screenshot(filename) {
    const png = await app.evaluate(async ({ BrowserWindow }) => {
      const image =
        await BrowserWindow.getAllWindows()[0].webContents.capturePage(
          undefined,
          { stayHidden: true, stayAwake: true },
        );
      return image.toPNG().toString("base64");
    });
    assert(
      png.length > 10000,
      "Screenshot must contain the rendered workbench",
    );
    fs.writeFileSync(
      path.join(root, "test-results", filename),
      Buffer.from(png, "base64"),
    );
  }
  try {
    await page
      .getByRole("heading", { name: "从一个问题或任务开始。" })
      .waitFor();
    // Global model setup works before any project is opened.
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    const settingsDialog = page.getByRole("dialog", { name: "工作台设置" });
    await settingsDialog
      .getByLabel("API 协议", { exact: true })
      .selectOption("openai");
    await settingsDialog
      .getByLabel("模型 ID", { exact: true })
      .fill("desktop-test");
    await settingsDialog
      .getByLabel("API 地址", { exact: true })
      .fill(baseUrl + "/chat/completions");
    await settingsDialog
      .locator('input[type="password"]')
      .fill("local-test-key");
    assert(
      await settingsDialog
        .getByRole("button", { name: "保存配置" })
        .isEnabled(),
    );
    await settingsDialog.getByRole("button", { name: "保存配置" }).click();
    await settingsDialog.waitFor({ state: "hidden" });
    assert.equal(
      JSON.parse(fs.readFileSync(path.join(config, "config.json"), "utf8"))
        .openai_base_url,
      baseUrl,
    );
    assert.equal(received.length, 0, "Saving settings must not call the model");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    assert.equal(
      await settingsDialog.getByLabel("API 地址", { exact: true }).inputValue(),
      baseUrl,
    );
    assert.equal(
      await settingsDialog.locator('input[type="password"]').inputValue(),
      "",
    );
    assert.equal(await settingsDialog.getByLabel("API Key", { exact: true }).getAttribute("placeholder"), "••••••••");
    await settingsDialog.getByText("已保存", { exact: true }).waitFor();
    assert.equal(await settingsDialog.getByLabel("连接方式", { exact: true }).inputValue(), "direct");
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page
      .locator(".project-item")
      .filter({ hasText: "sample-project" })
      .click();
    await page.getByRole("heading", { name: "今天想完成什么？" }).waitFor();
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByText("project-review", { exact: true }).waitFor();
    await page.locator(".capability-card").filter({ has: page.getByText("project-review", { exact: true }) }).getByRole("button", { name: "查看指令", exact: true }).click();
    await page
      .getByRole("dialog", { name: "技能指令" })
      .getByText(/DESKTOP_SKILL_PROOF/)
      .waitFor();
    await page.getByRole("button", { name: "关闭技能预览" }).click();
    await page.locator(".capability-card").filter({ has: page.getByText("project-review", { exact: true }) }).getByRole("button", { name: "选择使用" }).click();
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("验收技能：请分析这个项目。");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page
      .getByText("桌面验收完成：项目已挂载，技能已加载，模型流式连接正常。", {
        exact: true,
      })
      .waitFor();
    await page
      .getByRole("button", { name: "停止任务" })
      .waitFor({ state: "hidden" });
    assert(
      received.some((body) =>
        JSON.stringify(body.messages).includes("DESKTOP_SKILL_PROOF"),
      ),
    );
    assert(
      received.some((body) =>
        JSON.stringify(body.messages).includes("Skill directory:"),
      ),
    );
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("验收文件：请创建 desktop-proof.txt。");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page.getByRole("button", { name: "允许一次" }).waitFor();
    assert.equal(
      fs.existsSync(path.join(project, "desktop-proof.txt")),
      false,
      "File must not be written before approval",
    );
    await page.getByRole("button", { name: "允许一次" }).click();
    await page
      .getByRole("button", { name: "停止任务" })
      .waitFor({ state: "hidden" });
    assert.equal(
      fs.readFileSync(path.join(project, "desktop-proof.txt"), "utf8"),
      "Created only after desktop approval.",
    );
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("验收子代理：请并行启动两个 reviewer。");
    await page.getByRole("button", { name: "发送任务" }).click();
    const handledApprovals = new Set();
    for (let attempt = 0; attempt < 150; attempt++) {
      // The permission card was exercised above. Route parallel worker approvals
      // by their IDs over the same real IPC API to avoid clicking a stale card.
      for (const approval of events.filter(
        (event) =>
          event.type === "permission" &&
          event.request?.tool_name === "agent_tool",
      )) {
        if (handledApprovals.has(approval.requestId)) continue;
        await page.evaluate(
          ({ session, requestId }) =>
            window.xuloom.invoke("respond", {
              session,
              requestId,
              answer: "allow",
            }),
          approval,
        );
        handledApprovals.add(approval.requestId);
      }
      const done = await page.evaluate(
        () =>
          document.querySelector('[aria-label="停止任务"]') === null &&
          !document.querySelector(".worker-strip"),
      );
      if (done && received.filter(isChildRequest).length >= 2) break;
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "代理", exact: true }).click();
    await page.locator(".inspector").getByText("已完成", { exact: true }).first().waitFor();
    assert.equal(
      await page.locator(".inspector").getByText("已完成", { exact: true }).count(),
      2,
      "Both background workers must complete",
    );
    const childRequests = received.filter(isChildRequest);
    assert(childRequests.length >= 2);
    assert(
      childRequests.every(
        (body) =>
          !body.tools.some((tool) => tool.function.name === "file_write_tool"),
      ),
    );
    if (await page.getByRole("button", { name: "展开能力面板", exact: true }).count()) await page.getByRole("button", { name: "展开能力面板", exact: true }).click();
    await page.getByRole("button", { name: "Skills", exact: true }).click();
    await screenshot("workbench.png");
    // A second project owns a distinct Python process and capability catalog.
    await page
      .getByRole("button", { name: "打开项目 another-project", exact: true })
      .click();
    await page.locator(".capability-card strong").filter({ hasText: /^project-review$/ }).waitFor({ state: "hidden" });
    await page.getByText("code-review", { exact: true }).waitFor();
    assert.equal(await page.locator(".capability-card").count(), 9, "Another project has nine public bundled skills but not the first project's skill");
    await page
      .getByRole("textbox", { name: "任务输入" })
      .fill("第二个项目的独立会话");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page
      .getByRole("button", { name: "停止任务" })
      .waitFor({ state: "hidden" });
    assert(
      fs
        .readdirSync(path.join(historyRoot(secondProject, data), "sessions"))
        .some((name) => name.endsWith(".json")),
    );
    assert.equal(
      fs.existsSync(path.join(secondProject, "desktop-proof.txt")),
      false,
    );
    // Unload the runtime, then reopen the same durable row: no duplicate history.
    await page.locator(".session-select").filter({ hasText: "验收技能：请分析这个项目。" }).click();
    await page.locator(".session-item.active").filter({ hasText: "验收技能：请分析这个项目。" }).waitFor();
    const originalId = await page.locator(".session-item.active").getAttribute("data-session-id");
    const rowsBefore = await page.locator(".session-item").count();
    await page.evaluate((session) => window.xuloom.invoke("session.close", { session }), originalId);
    await page.locator(".session-select").filter({ hasText: "验收技能：请分析这个项目。" }).click();
    await page.locator(".session-item.active").filter({ hasText: "验收技能：请分析这个项目。" }).waitFor();
    assert.equal(await page.locator(".session-item").count(), rowsBefore);
    assert.equal(await page.locator(".session-item.active").getAttribute("data-session-id"), originalId);
    assert.deepEqual(errors, []);
    // Relaunch with the same data, without clicking Open project. All open
    // sessions, the selected conversation, and project history must restore.
    const requestsBeforeRestart = received.length;
    const savedWorkspace = JSON.parse(fs.readFileSync(path.join(data, "workbench.json"), "utf8")).workspace;
    assert(savedWorkspace.some((entry) => entry.resumeId && entry.active));
    await app.close();
    app = await electron.launch(launchOptions);
    page = await app.firstWindow();
    page.setDefaultTimeout(45000);
    await page.getByRole("textbox", { name: "任务输入" }).waitFor();
    await page.locator(".session-select").filter({ hasText: "验收技能：请分析这个项目。" }).waitFor();
    assert((await page.locator(".session-item.active").innerText()).includes("验收技能"));
    assert.equal(await page.locator(".session-item").count(), rowsBefore);
    assert.equal(received.length, requestsBeforeRestart, "Restoring history must not call the model");
    await page.getByRole("button", { name: "设置与模型", exact: true }).click();
    await settingsDialog.getByPlaceholder("••••••••", { exact: true }).waitFor();
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page.getByRole("textbox", { name: "任务输入" }).fill("重启之后继续会话");
    await page.getByRole("button", { name: "发送任务" }).click();
    await page.getByRole("button", { name: "停止任务" }).waitFor({ state: "hidden" });
    assert(received.length > requestsBeforeRestart);
    assert(received.slice(requestsBeforeRestart).some((body) => body.stream &&
      body.messages.some((message) => messageText(message).includes("验收技能：请分析这个项目。"))));
    assert(
      requestedPaths.length > 0 &&
        requestedPaths.every((url) => url === "/v1/chat/completions"),
    );
    console.log(
      "PASS: desktop launch, proxy bypass, saved Key indicator, skills, permissions, background agents, project isolation, history restored after restart, continued conversation",
    );
    console.log("Screenshot: " + path.join(root, "test-results/workbench.png"));
  } catch (error) {
    fs.writeFileSync(
      path.join(root, "test-results/requests.json"),
      JSON.stringify(received, null, 2),
    );
    console.error(
      "Event timeline: " +
        events
          .map(
            (event) =>
              event.type + (event.requestId ? ":" + event.requestId : ""),
          )
          .join(", "),
    );
    await screenshot("failure.png");
    console.error("Fixture retained at " + fixture);
    console.error(
      "Page: " + (await page.locator("body").innerText()).slice(-12000),
    );
    throw error;
  } finally {
    await app.close();
    server.close();
  }
}
fs.mkdirSync(path.join(root, "test-results"), { recursive: true });
main().catch((error) => {
  console.error(error);
  server.close();
  process.exitCode = 1;
});
