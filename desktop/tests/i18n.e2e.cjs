// English UI: switch the language, then check the main surfaces render without leftover Chinese.
// XULOOM_I18N_REPORT=1 lists every remaining Chinese string instead of failing (used to extend locales/en.ts).
const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-i18n-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), project = path.join(fixture, 'project');
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), 'resources/python/python.exe') : process.env.XULOOM_PYTHON || 'python';
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: project, python }));
const report = process.env.XULOOM_I18N_REPORT === '1';
const errors = [], leftovers = new Map();
let app, page;
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', chunk => raw += chunk);
  req.on('end', () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content: '[]' } }] }));
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    for (const [delta, finish_reason] of [[{ content: '模型回复保持原文 MODEL_TEXT' }, null], [{}, 'stop']])
      res.write('data: ' + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
    res.write('data: ' + JSON.stringify({ choices: [], usage: { prompt_tokens: 100, completion_tokens: 20 } }) + '\n\n');
    res.end('data: [DONE]\n\n');
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_TEST_HEADLESS: '1', XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: '', HOME: home, USERPROFILE: home, XULOOM_NETWORK_MODE: 'direct' };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'PYTHONHOME', 'PYTHONPATH']) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on('pageerror', error => errors.push(error.stack));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
}
async function shot(name) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
  fs.writeFileSync(path.join(root, 'test-results', name), Buffer.from(png, 'base64'));
}
// Visible UI text and labels that still contain Chinese (model output and user input are excluded on purpose).
async function collect(screen) {
  const found = await page.evaluate(() => {
    const han = /[一-鿿]/, out = new Set();
    const skip = node => node.closest && node.closest('.markdown, pre, code, textarea, input, select, [contenteditable="true"]');
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || skip(parent) || !parent.getClientRects().length) continue;
      if (han.test(node.nodeValue)) out.add(node.nodeValue.trim());
    }
    for (const element of document.querySelectorAll('[aria-label], [title], [placeholder]')) {
      if (element.closest('.markdown, pre, code')) continue;
      for (const name of ['aria-label', 'title', 'placeholder']) { const value = element.getAttribute(name); if (value && han.test(value)) out.add(value.trim()); }
    }
    return [...out];
  });
  for (const text of found) if (!leftovers.has(text)) leftovers.set(text, screen);
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ active_model_connection: 'owned', model_provider: 'openai', model_connections: [{
    id: 'owned', name: 'Owned fixture', provider: 'openai', model: 'fixture', models: ['fixture'], api_key: 'owned-key', base_url: `http://127.0.0.1:${server.address().port}/v1`,
  }] }));
  try {
    await launch();
    // Default stays Chinese; switch through Settings → Workspace.
    await page.getByRole('button', { name: '设置与模型' }).click();
    await page.locator('#settings-tab-workspace').click();
    await Promise.all([page.waitForEvent('load'), page.locator('.language-setting select').selectOption('en')]);
    await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'en');
    await page.getByRole('button', { name: 'New conversation' }).first().waitFor();
    await collect('welcome'); await shot('i18n-welcome.png');

    await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    await page.getByRole('textbox', { name: 'Task input' }).fill('hello');
    await page.getByRole('button', { name: 'Send task', exact: true }).click();
    await page.getByText('模型回复保持原文 MODEL_TEXT').waitFor();   // model output is never translated
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    await collect('conversation'); await shot('i18n-conversation.png');

    await page.getByRole('button', { name: 'Add tools and modes', exact: true }).click();
    await collect('composer menu');
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: 'Settings & models' }).click();
    await page.locator('#settings-tab-model').waitFor();
    for (const tab of await page.locator('[id^="settings-tab-"]').all()) {
      await tab.click();
      await page.waitForTimeout(150);
      await collect('settings: ' + (await tab.innerText()).trim());
    }
    await page.locator('#settings-tab-model').click();
    await shot('i18n-settings.png');
    // Switching back restores Chinese.
    await page.locator('#settings-tab-workspace').click();
    await Promise.all([page.waitForEvent('load'), page.locator('.language-setting select').selectOption('zh')]);
    await page.getByRole('button', { name: '新建会话' }).first().waitFor();

    if (report) {
      console.log(JSON.stringify([...leftovers.entries()], null, 1));
    } else {
      assert.deepEqual([...leftovers.keys()], [], 'Untranslated UI text: ' + JSON.stringify([...leftovers.entries()]));
    }
    assert.deepEqual(errors, []);
    console.log('I18N_E2E_PASS');
  } finally {
    if (app) await app.close().catch(() => {});
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
