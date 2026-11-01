const test = require("node:test"), assert = require("node:assert/strict");
const { MediaPreviews } = require("../electron/media-preview.cjs");

function fixture() {
  const content = Buffer.from("a".repeat(600000)), calls = [];
  const file = { path: "/remote/work/generated.wav", mime: "audio/wav", size: content.length, version: "a".repeat(64) };
  const transport = { async request(method, input) {
    calls.push({ method, input });
    if (method === "files.mediaInfo") return file;
    const bytes = content.subarray(input.offset, input.offset + input.length);
    return { offset: input.offset, nextOffset: input.offset + bytes.length, size: file.size, version: file.version, data: bytes.toString("base64") };
  } };
  return { content, calls, file, transport, previews: new MediaPreviews() };
}

test("audio/video preview streams bounded RPC chunks from its actual execution machine", async () => {
  const f = fixture(), view = await f.previews.create("ssh:fixture", f.transport, f.file.path);
  const response = await f.previews.respond(new Request(view.url));
  assert.equal(response.status, 200); assert.equal(response.headers.get("content-type"), "audio/wav");
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), f.content);
  assert(f.calls.filter(call => call.method === "files.mediaChunk").length >= 3);
  for (const call of f.calls) { assert.equal(call.input.path, "/remote/work/generated.wav"); if (call.method === "files.mediaChunk") assert(call.input.length <= 262144); }
  assert(!view.url.includes("remote/work"));
});

test("range, suffix and HEAD obey the browser media contract without full file buffers", async () => {
  const f = fixture(), view = await f.previews.create("session", f.transport, f.file.path);
  for (const [range, expectedStart, expectedEnd] of [["bytes=7-19", 7, 19], ["bytes=-10", 599990, 599999], ["bytes=599999-999999", 599999, 599999]]) {
    const response = await f.previews.respond(new Request(view.url, { headers: { Range: range } }));
    assert.equal(response.status, 206); assert.equal(response.headers.get("content-range"), `bytes ${expectedStart}-${expectedEnd}/600000`);
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), f.content.subarray(expectedStart, expectedEnd + 1));
  }
  const before = f.calls.length;
  const head = await f.previews.respond(new Request(view.url, { method: "HEAD" }));
  assert.equal(head.status, 200); assert.equal((await head.arrayBuffer()).byteLength, 0); assert.equal(f.calls.length, before);
  for (const range of ["bytes=600000-", "bytes=10-2", "bytes=-0", "bytes=0-1,3-4", "anything"]) {
    const response = await f.previews.respond(new Request(view.url, { headers: { range } })); assert.equal(response.status, 416);
  }
  assert.equal(f.calls.length, before);
});

test("only the owning session can revoke a token and revoked streams cannot continue", async () => {
  const f = fixture(), view = await f.previews.create("owner", f.transport, f.file.path);
  f.previews.revoke(view.token, "another"); assert(f.previews.pages.has(view.token));
  let release, started;
  const ready = new Promise(resolve => started = resolve), gate = new Promise(resolve => release = resolve);
  const original = f.transport.request;
  f.transport.request = async (...args) => { started(); await gate; return original(...args); };
  const response = await f.previews.respond(new Request(view.url));
  const reading = response.arrayBuffer(); await ready;
  f.previews.revokeSession("owner"); release();
  await assert.rejects(reading, /关闭/);
  assert.equal((await f.previews.respond(new Request(view.url))).status, 404);
});

test("preview rejects changed versions, incorrect receipts, unwanted types and URL operations", async () => {
  const f = fixture(), view = await f.previews.create("session", f.transport, f.file.path);
  f.transport.request = async () => ({ offset: 0, nextOffset: 3, size: f.file.size, version: "b".repeat(64), data: "YWJj" });
  const response = await f.previews.respond(new Request(view.url));
  await assert.rejects(response.arrayBuffer(), /回执无效/);
  assert.equal((await f.previews.respond(new Request(view.url, { method: "POST" }))).status, 405);
  assert.equal((await f.previews.respond(new Request(view.url + "?path=/private"))).status, 404);
  for (const mime of ["text/html", "application/javascript", "video/mp4\r\nX-Evil: yes"]) {
    const bad = fixture(); bad.file.mime = mime;
    await assert.rejects(bad.previews.create("session", bad.transport, bad.file.path), /请选择/);
  }
});
