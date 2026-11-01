const test = require("node:test"), assert = require("node:assert/strict");
const { parseLink, chatLink, startupLinks, remoteKey } = require("../electron/chat-links.cjs");
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

test("SSH addresses share canonical target identities with Python without embedding hosts or paths", () => {
  const profile={id:'lab',name:'名称',host:'user@alias',port:null,cwd:'/home/科学',python:'python3',platform:'posix',ssh_config:''};
  const key=remoteKey(profile);
  assert.equal(key,'6d61e4e926e6a8a6aa62e24f3c97cef7190b6d286fc34ca3fb07ca05c5932231');
  assert.equal(remoteKey({...profile,name:'renamed'}),key);
  const url=chatLink(`ssh:${key}:remote-chat`);
  assert.equal(url,`xuloom://chat/ssh/v1/${key}/remote-chat`);
  assert.deepEqual(parseLink(url),{key,sessionId:'remote-chat',remote:true});
  assert(!url.includes(profile.host));assert(!url.includes(profile.cwd));
  for(const suffix of ['\n','?run=abc','#part','/','%20'])assert.throws(()=>parseLink(url+suffix));
});
