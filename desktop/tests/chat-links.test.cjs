const test = require("node:test"), assert = require("node:assert/strict");
const { parseLink, chatLink, startupLinks } = require("../electron/chat-links.cjs");
test("chat links contain only a workspace identity and session ID", () => {
  const url = chatLink("0123456789abcdef:legacy_chat-1");
  assert.equal(url, "xuloom://chat/v1/0123456789abcdef/legacy_chat-1");
  assert.deepEqual(parseLink(url), { key: "0123456789abcdef", sessionId: "legacy_chat-1" });
});
test("chat addresses reject commands, paths, credentials and encoded selectors", () => {
  const valid = chatLink("0123456789abcdef:session");
  for (const value of [valid + "\n", valid + "?run=abc", valid + "#section", valid + "/", valid.replace("session", "%2e%2e"),
    valid.replace("chat/v1", "chat/v2"), valid.replace("chat/v1", "user:password@chat/v1"), "https://example.org", null, valid + "x".repeat(513)])
    assert.throws(() => parseLink(value));
});
test("startup accepts only bounded Xuloom arguments without interpreting them", () => {
  const valid = chatLink("0123456789abcdef:session");
  assert.deepEqual(startupLinks(["Xuloom.exe", "--inspect", "https://example.org", valid]), [valid]);
  assert.equal(startupLinks(Array(40).fill(valid)).length, 16);
});
