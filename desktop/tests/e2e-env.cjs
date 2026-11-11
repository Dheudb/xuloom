// Keep desktop fixtures independent of the shell launching them. Runtime paths
// are explicit test inputs; model credentials, user state and policies are not.
function fixtureEnv({ data, config, home }, base = process.env) {
  const env = Object.fromEntries(Object.entries(base).filter(([key]) =>
    !/^(OPENAI_|ANTHROPIC_|XULOOM_|PYTHONPATH$|PYTHONHOME$|ELECTRON_RUN_AS_NODE$|RIPGREP_CONFIG_PATH$|(?:HTTP|HTTPS|ALL|NO)_PROXY$)/i.test(key)));
  if (base.XULOOM_PYTHON) env.XULOOM_PYTHON = base.XULOOM_PYTHON;
  return { ...env, HOME: home, USERPROFILE: home, XULOOM_USER_DATA: data,
    XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1",
    XULOOM_NETWORK_MODE: "direct", XULOOM_SKILLS_PATHS: "" };
}

module.exports = { fixtureEnv };
