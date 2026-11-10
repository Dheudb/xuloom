const test = require("node:test");
const assert = require("node:assert/strict");
const { createPluginMethods } = require("../electron/plugin-methods.cjs");

test("plugin RPC methods extend the allowlist from the engine's registry only", async () => {
  const calls = [];
  const methods = createPluginMethods({
    builtin: new Set(["prompt", "files.read"]),
    request: async (method) => {
      calls.push(method);
      return { methods: [
        { name: "echo-fixture.echo" },
        { name: "media.control", operation: true },
        { name: "media.save", settings: true },
        { name: "files.read", operation: true },   // cannot shadow a built-in
        { name: "../escape" }, { name: 42 }, null,  // malformed entries are ignored
      ] };
    },
  });
  assert.deepEqual(await methods.lookup("echo-fixture.echo"), { operation: false, settings: false });
  assert.deepEqual(await methods.lookup("media.control"), { operation: true, settings: false });
  assert.deepEqual(await methods.lookup("media.save"), { operation: false, settings: true });
  assert.equal(await methods.lookup("files.read"), null);
  assert.equal(await methods.lookup("not-registered.call"), null);
  assert.equal(await methods.lookup("no-dot"), null);
  assert.deepEqual((await methods.list()).map((item) => item.name), ["echo-fixture.echo", "media.control", "media.save"]);
  assert.deepEqual(calls, ["plugins.rpc"]);  // fetched once
});
