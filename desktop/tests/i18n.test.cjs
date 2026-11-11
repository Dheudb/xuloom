const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

async function english() {
  // The language is read once when the module loads, like a page load after switching.
  globalThis.localStorage = { getItem: key => key === "xuloom.language" ? "en" : null, setItem() {} };
  return import("../src/i18n.ts?en");
}

test("Chinese is the source language: messages are returned as written, with values filled in", async () => {
  delete globalThis.localStorage;
  const { t, tServer, displayName } = await import("../src/i18n.ts?zh");
  assert.equal(t("会话操作 {title}", { title: "设置" }), "会话操作 设置");
  assert.equal(tServer("引擎发生错误"), "引擎发生错误");
  assert.equal(displayName("新会话"), "新会话");
});

test("placeholders are never translated, even when they equal interface words", async () => {
  const { t } = await english();
  assert.equal(t("会话操作 {title}", { title: "设置" }), "Conversation actions 设置");
  assert.equal(t("在项目 {name} 中新建会话", { name: "费用" }), "New conversation in project 费用");
  assert.equal(t("设置"), "Settings");
  assert.equal(t("没有英文的新文案 {x}", { x: 1 }), "没有英文的新文案 1", "unknown messages fall back to Chinese, never to a partial translation");
});

test("plural forms and literal braces", async () => {
  const { t } = await english();
  assert.equal(t("流水线 · {length} 项", { length: 1 }), "Pipeline · 1 item");
  assert.equal(t("流水线 · {length} 项", { length: 3 }), "Pipeline · 3 items");
  assert.equal(t("{json}", {}), "{json}", "a brace that is not a known placeholder stays literal");
});

test("tx keeps elements inside the sentence", async () => {
  const { tx } = await english();
  const link = { type: "a", props: {}, key: null };
  const node = tx("删除「{value}」及其 {length} 个模型配置？保存的 Key 也会删除，聊天记录会保留。", { value: link, length: 2 });
  assert.equal(typeof node, "object");
  assert(node.props.children.includes(link));
  assert.equal(tx("设置"), "Settings", "plain sentences stay strings");
});

test("stored names stay verbatim even when they equal application defaults", async () => {
  const { displayName } = await english();
  for (const name of ["新会话", "我的 API", "新 API", "设置"]) assert.equal(displayName(name), name);
  assert.equal(displayName("设置"), "设置");
});

test("backend text: known messages are translated, paths and quoted names inside them are kept", async () => {
  const { tServer } = await english();
  assert.equal(tServer("任务已停止，已收到的内容保留。"), "Task stopped; everything received so far is kept.");
  const message = tServer("已恢复会话：C:\\项目\\设置.md");
  assert(message.includes("C:\\项目\\设置.md"), message);
  assert.equal(tServer("完全未知的后端消息"), "完全未知的后端消息");
});

test("every tool title (verb x state) has English", async () => {
  const { english: catalog } = await import("../src/locales/en.ts");
  const source = require("node:fs").readFileSync(path.join(__dirname, "../src/tool-presentation.ts"), "utf8");
  const verbs = [...source.matchAll(/N_\("([^"]+)"\)/g)].map(match => match[1]);
  assert(verbs.length > 20);
  for (const verb of verbs) for (const key of [verb, "已" + verb, verb + "失败", verb + "已取消"]) assert(key in catalog, key);
});

test("interface text is wrapped in t()/tx() and every message has English", () => {
  execFileSync(process.execPath, [path.join(__dirname, "../scripts/i18n-codemod.mjs"), "--check"], { stdio: "pipe" });
});
