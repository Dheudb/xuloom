const test = require("node:test");
const assert = require("node:assert/strict");
const { fixtureEnv } = require("./e2e-env.cjs");

test("E2E fixtures cannot inherit host credentials, state, policies or search configuration", () => {
  const base = { PATH: "runtime-path", SystemRoot: "windows", XULOOM_PYTHON: "test-python",
    ANTHROPIC_AUTH_TOKEN: "host-key", OPENAI_FALLBACK_MODEL: "host-model",
    XULOOM_DATA_DIR: "host-history", XULOOM_MAX_COST_USD: "0", XULOOM_DEV_URL: "host-ui",
    PYTHONPATH: "host-packages", ELECTRON_RUN_AS_NODE: "1", HTTPS_PROXY: "host-proxy",
    RIPGREP_CONFIG_PATH: "host-search", anthropic_api_key: "lowercase-key" };
  const before = { ...base };
  const env = fixtureEnv({ data: "fixture-data", config: "fixture-config", home: "fixture-home" }, base);
  assert.equal(env.PATH, base.PATH);
  assert.equal(env.SystemRoot, base.SystemRoot);
  assert.equal(env.XULOOM_PYTHON, "test-python");
  assert.equal(env.XULOOM_DATA_DIR, "fixture-data");
  assert.equal(env.XULOOM_USER_DATA, "fixture-data");
  assert.equal(env.XULOOM_CONFIG_DIR, "fixture-config");
  assert.equal(env.HOME, "fixture-home");
  assert.equal(env.USERPROFILE, "fixture-home");
  assert.equal(env.XULOOM_NETWORK_MODE, "direct");
  for (const key of ["ANTHROPIC_AUTH_TOKEN", "OPENAI_FALLBACK_MODEL", "XULOOM_MAX_COST_USD", "XULOOM_DEV_URL",
    "PYTHONPATH", "ELECTRON_RUN_AS_NODE", "HTTPS_PROXY", "RIPGREP_CONFIG_PATH", "anthropic_api_key"]) assert(!Object.hasOwn(env, key), key);
  assert.deepEqual(base, before, "Sanitizing a fixture must never change the launching shell");
});
