// English UI must never translate user or model content: conversation titles, project names,
// connection names, file names and messages that happen to equal UI words stay exactly as written.
const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-i18n-content-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
const project = path.join(fixture, '费用'), standalone = path.join(fixture, 'standalone');
for (const directory of [data, config, home, project, standalone]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), 'resources/python/python.exe') : process.env.XULOOM_PYTHON || 'python';
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [project], defaultDirectory: standalone, python }));
fs.writeFileSync(path.join(data, 'preferences.json'), '{}');
const errors = [];
let app, page, streams = 0;
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', chunk => raw += chunk);
  req.on('end', () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content: '[]' } }] }));
    streams += 1;
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
    if (streams === 1) {
      chunk({ role: 'assistant', tool_calls: [{ index: 0, id: 'call-1', type: 'function', function: { name: 'file_write_tool', arguments: JSON.stringify({ file_path: '设置.md', content: '内容' }) } }] });
      chunk({}, 'tool_calls');
    } else {
      chunk({ role: 'assistant', content: '完成' }); chunk({}, 'stop');
    }
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
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ active_model_connection: 'owned', model_provider: 'openai', model_connections: [{
    id: 'owned', name: '设置', provider: 'openai', model: 'fixture', models: ['fixture'], api_key: 'owned-key', base_url: `http://127.0.0.1:${server.address().port}/v1`,
  }] }));
  try {
    await launch();
    await page.evaluate(() => localStorage.setItem('xuloom.language', 'en'));
    await Promise.all([page.waitForEvent('load'), page.evaluate(() => window.xuloom.reloadWindow())]);
    await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
    assert.equal(await page.locator('.project-new').first().getAttribute('aria-label'), 'New conversation in project 费用', 'mixed labels keep the project name');
    await page.locator('.project-new').first().click();
    await page.locator('.session-item.active').waitFor();
    await page.getByRole('textbox', { name: 'Task input' }).fill('设置');
    await page.getByRole('button', { name: 'Send task', exact: true }).click();
    await page.getByRole('button', { name: 'Allow once', exact: true }).click();
    await page.getByText('完成', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    const id = await page.locator('.session-item.active').getAttribute('data-session-id');
    await page.evaluate(id => window.xuloom.invoke('session.update', { id, patch: { title: '设置' } }), id);
    await page.locator('.session-item.active strong').filter({ hasText: /^设置$/ }).waitFor();

    const view = await page.evaluate(() => ({
      projects: [...document.querySelectorAll('.project-item span')].map(node => node.textContent),
      session: document.querySelector('.session-item.active strong')?.textContent,
      sessionMenu: document.querySelector('.session-item.active .row-menu-trigger')?.getAttribute('aria-label'),
      prompt: [...document.querySelectorAll('.user-prompt-text')].map(node => node.textContent),
      files: document.body.innerText.includes('设置.md'),
      settingsWord: document.body.innerText.includes('Settings'),
    }));
    assert.deepEqual(view.projects, ['费用'], 'project names stay verbatim');
    assert.equal(view.session, '设置', 'conversation titles stay verbatim');
    assert.equal(view.sessionMenu, 'Conversation actions 设置', 'labels translate UI words but keep the name');
    assert.deepEqual(view.prompt, ['设置'], 'user messages stay verbatim');
    assert(view.files, 'file names stay verbatim');

    await page.evaluate(id => window.xuloom.invoke('session.update', { id, patch: { title: '新会话' } }), id);
    await page.locator('.session-item.active strong').filter({ hasText: /^新会话$/ }).waitFor();
    assert.equal(await page.locator('.session-item.active .row-menu-trigger').getAttribute('aria-label'), 'Conversation actions 新会话');

    await page.getByRole('button', { name: 'Settings & models' }).click();
    await page.locator('#settings-tab-model').click();
    const options = await page.locator('select[aria-label="API connection"] option').allTextContents();
    assert(options.includes('设置'), 'connection names stay verbatim: ' + JSON.stringify(options));
    assert.deepEqual(errors, []);
    console.log('I18N_CONTENT_E2E_PASS');
  } finally {
    if (app) await app.close().catch(() => {});
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
