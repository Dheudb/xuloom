// Per-turn token/cost display and the 费用 settings page (prices + task cap in ~/.xuloom/pricing.json).
const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-cost-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), project = path.join(fixture, 'project');
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), 'resources/python/python.exe') : process.env.XULOOM_PYTHON || 'python';
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: project, python }));
const errors = [];
let app, page, answers = 0;
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', chunk => raw += chunk);
  req.on('end', () => {
    try {
      const body = JSON.parse(raw);
      if (!body.stream) return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content: '[]' } }], usage: { prompt_tokens: 10, completion_tokens: 2 } }));
      answers += 1;
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const [delta, finish_reason] of [[{ content: `COST_ANSWER_${answers}` }, null], [{}, 'stop']])
        res.write('data: ' + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
      res.write('data: ' + JSON.stringify({ choices: [], usage: { prompt_tokens: 1200, completion_tokens: 300 } }) + '\n\n');
      res.end('data: [DONE]\n\n');
    } catch (error) { errors.push(error.stack); res.writeHead(500).end('Owned fixture failed'); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_TEST_HEADLESS: '1', XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: '', HOME: home, USERPROFILE: home, XULOOM_NETWORK_MODE: 'direct' };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER', 'XULOOM_MAX_COST_USD', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'PYTHONHOME', 'PYTHONPATH']) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on('pageerror', error => errors.push(error.stack));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
}
async function shot(name) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  fs.writeFileSync(path.join(root, 'test-results', name), Buffer.from(png, 'base64'));
}
async function ask(text, answer) {
  await page.getByRole('textbox', { name: '任务输入' }).fill(text);
  await page.getByRole('button', { name: '发送任务', exact: true }).click();
  await page.getByText(answer, { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
  return (await page.locator('[data-turn-usage]').last().innerText()).trim();
}
async function savePricing(rows, cap) {
  await page.getByRole('button', { name: '设置与模型' }).click();
  await page.getByRole('tab', { name: '费用' }).click();
  const panel = page.locator('#settings-panel-cost');
  await panel.getByRole('table', { name: '模型价格' }).waitFor();
  while (await panel.getByLabel('模型名称').count()) await panel.getByRole('button', { name: /^删除/ }).first().click();
  for (const [model, input, output] of rows) {
    await panel.getByRole('button', { name: '添加模型价格' }).click();
    await panel.getByLabel('模型名称').last().fill(model);
    await panel.getByLabel('输入价格').last().fill(input);
    await panel.getByLabel('输出价格').last().fill(output);
  }
  await panel.getByLabel('任务花费上限').fill(cap);
  await panel.getByRole('button', { name: '保存', exact: true }).click();
  await panel.getByRole('button', { name: '已保存' }).waitFor();
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ active_model_connection: 'owned', model_provider: 'openai', model_connections: [{
    id: 'owned', name: 'Owned fixture', provider: 'openai', model: 'fixture', models: ['fixture'], api_key: 'owned-key', base_url: `http://127.0.0.1:${server.address().port}/v1`,
  }] }));
  try {
    await launch(); await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    // 1. No price yet: tokens only, clearly marked as unpriced (never shown as $0).
    assert.equal(await ask('first', 'COST_ANSWER_1'), '1,500 tokens · 未计价');

    // 2. Price it in Settings → 费用; the open conversation uses it from its next request.
    await savePricing([['fixture', '1000', '1000']], '');
    await shot('cost-settings.png');
    const saved = JSON.parse(fs.readFileSync(path.join(home, '.xuloom', 'pricing.json'), 'utf8'));
    assert.deepEqual(saved, { models: { fixture: { input: 1000, output: 1000 } } });
    await page.getByRole('button', { name: '关闭设置' }).click();
    assert.equal(await ask('second', 'COST_ANSWER_2'), '1,500 tokens · $1.50');

    // 3. Local model priced at 0 with a cap: runs and shows $0.00 instead of being refused as unpriced.
    await savePricing([['fix*', '0', '0']], '0.5');
    assert.equal(JSON.parse(fs.readFileSync(path.join(home, '.xuloom', 'pricing.json'), 'utf8')).max_cost_usd, 0.5);
    await page.getByRole('button', { name: '关闭设置' }).click();
    assert.equal(await ask('third', 'COST_ANSWER_3'), '1,500 tokens · $0.00');
    await shot('cost-turn-usage.png');
    const title = await page.locator('[data-turn-usage]').last().getAttribute('title');
    assert(title.includes('输入 1,200 / 输出 300 tokens'), title);
    assert.deepEqual(errors, []);
    console.log('COST_E2E_PASS');
  } finally {
    if (app) await app.close().catch(() => {});
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
