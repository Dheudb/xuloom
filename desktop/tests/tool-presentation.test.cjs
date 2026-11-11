const test = require("node:test");
const assert = require("node:assert/strict");
test("tool transcript uses semantic labels without hiding failures", async () => {
  const { toolPresentation: view } = await import("../src/tool-presentation.ts");
  const tool = (name, input = {}, status = "completed", extra = {}) => view({ name, input, status, ...extra });
  assert.deepEqual(tool("file_read_tool", { file_path: "C:\\work\\photo.PNG" }), { kind: "image", title: "已查看图像", brief: "photo.PNG", uiBrief: false });
  assert.equal(tool("custom_tool", {}, "completed", { media: [{ kind: "image", id: "fixture" }] }).kind, "image");
  assert.deepEqual(tool("browser_tool", { action: "render", file_path: "/work/page.html" }), { kind: "image", title: "已截图检查", brief: "page.html", uiBrief: false });
  assert.equal(tool("file_edit_tool", { file_path: "/work/main.py" }).kind, "edit");
  assert.equal(tool("command_tool", { command: "echo hello" }).brief, "echo hello");
  assert.equal(tool("command_tool", {}, "error").title, "运行命令失败");
  assert.equal(tool("command_tool", {}, "cancelled").title, "运行命令已取消");
  assert.equal(tool("grep_tool").kind, "search");
  assert.equal(tool("web_search_tool", { query: "query" }).title, "已搜索网页");
  assert.equal(tool("file_read_tool", { file_path: "notes.md" }).kind, "file");
  assert.equal(tool("web_fetch_tool").kind, "web");
  assert.equal(tool("spawn_agent").kind, "agent");
  assert.equal(tool("agent_tool", { action: "spawn" }, "running").title, "启动子代理");
  assert.equal(tool("agent_tool", { action: "wait" }, "running").title, "等待子代理");
  assert.equal(tool("skill_tool").kind, "skill");
  assert.equal(tool("unknown").brief, "unknown");
  // Briefs the UI writes itself may be translated; briefs that come from tool input never are.
  assert.equal(tool("tool_sequence", { steps: [{}, {}] }).uiBrief, true);
  assert.equal(tool("custom_tool", {}, "completed", { media: [{ kind: "image", id: "fixture" }] }).uiBrief, true);
  assert.equal(tool("browser_tool", { action: "screenshot" }).uiBrief, true);
  assert.equal(tool("command_tool", { command: "设置" }).uiBrief, false);
  assert.equal(tool("web_search_tool", { query: "费用" }).uiBrief, false);
});
