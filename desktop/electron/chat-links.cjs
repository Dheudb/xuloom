"use strict";
const pattern = /^xuloom:\/\/chat\/v1\/([0-9a-f]{16})\/([A-Za-z0-9_-]{1,128})$/;
const remotePattern = /^xuloom:\/\/chat\/ssh\/v1\/([0-9a-f]{64})\/([A-Za-z0-9_-]{1,128})$/;
const crypto = require("node:crypto");
const remoteKey = profile => crypto.createHash("sha256").update(JSON.stringify(Object.fromEntries(Object.entries(profile).filter(([key]) => key !== "name").sort(([a], [b]) => a.localeCompare(b))))).digest("hex");
const remoteId = value => typeof value === "string" && /^ssh:[0-9a-f]{64}:[A-Za-z0-9_-]{1,128}$/.test(value) && !/[\r\n]/.test(value);
function parseLink(value) {
  const match = typeof value === "string" && value.length <= 512 && (pattern.exec(value) || remotePattern.exec(value));
  // JS $ also matches before a final newline. Require the whole input.
  if (!match || match[0] !== value) throw new Error("无效的 Xuloom 聊天链接；仅支持本机 v1 或 SSH v1 会话地址");
  return { key: match[1], sessionId: match[2], ...(match[1].length === 64 ? { remote: true } : {}) };
}
function chatLink(id) {
  if (typeof id !== "string") throw new Error("会话 ID 无效");
  const value = remoteId(id) ? "xuloom://chat/ssh/v1/" + id.slice(4).replace(":", "/") : "xuloom://chat/v1/" + id.replace(":", "/");
  parseLink(value);
  return value;
}
function startupLinks(argv) {
  // Do not interpret an arbitrary argument as a command, path or web URL.
  return argv.filter(value => typeof value === "string" && /^xuloom:/i.test(value)).slice(0, 16);
}
module.exports = { parseLink, chatLink, startupLinks, remoteKey, remoteId };
