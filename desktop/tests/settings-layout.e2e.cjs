const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-settings-layout-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const directory of [data, config, home]) fs.mkdirSync(directory);
const requests = [], errors = [];
const server = http.createServer((req, res) => { requests.push(req.url); res.writeHead(404).end(); });
let app, page;
async function capture(name) {
  fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
  const png = await app.evaluate(async ({ BrowserWindow }) => {
    const web = BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
    await new Promise(resolve => setTimeout(resolve, 100));
    return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64');
  });
  fs.writeFileSync(path.join(root, 'test-results', name + '.png'), Buffer.from(png, 'base64'));
}
async function layout() {
  const value = await page.evaluate(() => {
    const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
    const scroll = document.querySelector('.model-form-body');
    return { width: innerWidth, height: innerHeight, advanced: document.querySelector('.model-options').open, dialog: rect('.settings-dialog'), footer: rect('.model-form-footer'),
      select: rect('[aria-label="API 连接"]'), add: rect('[aria-label="新增 API"]'), remove: rect('[aria-label="删除 API"]'),
      model: rect('[aria-label="模型 ID"]'), manage: rect('[aria-label="管理模型"]'),
      inputs: rect('[aria-label="模型输入能力"]'), reasoning: document.querySelector('[aria-label="推理参数格式"]') ? rect('[aria-label="推理参数格式"]') : null,
      overflow: scroll.scrollWidth > scroll.clientWidth + 1, outerOverflow: document.documentElement.scrollWidth > innerWidth };
  });
  assert(Math.abs(value.select.bottom - value.add.bottom) <= 1, 'Connection and add button must share a baseline');
  assert(Math.abs(value.select.bottom - value.remove.bottom) <= 1, 'Delete button must share the connection baseline');
  assert(Math.abs(value.model.bottom - value.manage.bottom) <= 1, 'Model input and management button must share a baseline');
  if (value.advanced && value.reasoning?.width) assert(Math.abs(value.inputs.left - value.reasoning.left) <= 1, 'Advanced model controls must share their left edge: ' + JSON.stringify([value.inputs.left, value.reasoning.left]));
  assert(!value.overflow && !value.outerOverflow, 'Settings must not overflow horizontally');
  assert(value.footer.bottom <= value.dialog.bottom + 1 && value.footer.bottom <= value.height, 'Save footer stays inside the dialog');
  assert(await page.getByRole('button', { name: '保存配置', exact: true }).isVisible());
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}/v1`;
  const profile = { adapter: 'openai', efforts: ['low', 'high'], canDisable: false, thinking: 'default', effort: 'high', budgetTokens: 2048 };
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ model_provider: 'openai', active_model_connection: 'fixture', model_connections: [
    { id: 'fixture', name: '我的研究连接', provider: 'openai', model: 'research-model', models: ['research-model', 'another-model'], base_url: base, api_key: 'owned-fixture-key',
      model_inputs: { 'research-model': ['text', 'image'] }, model_reasoning: { 'research-model': profile }, native_search: { backend: 'deepseek', model: '', baseUrl: '' } }
  ] }));
  try {
    const env = { ...process.env, XULOOM_TEST_HEADLESS: '1', XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, HOME: home, USERPROFILE: home, XULOOM_SKILLS_PATHS: '' };
    for (const name of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER']) delete env[name];
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
    page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on('pageerror', error => errors.push(error.message));
    await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
    await app.evaluate(({ BrowserWindow }) => { const window = BrowserWindow.getAllWindows()[0]; window.setMinimumSize(760, 600); window.setSize(1440, 940); });
    await page.getByRole('button', { name: '设置与模型', exact: true }).click();
    await page.getByRole('dialog', { name: '工作台设置' }).waitFor();
    for (const label of ['API 地址', 'API Key', '模型 ID']) assert(await page.getByLabel(label, { exact: true }).isVisible(), label + ' should be visible');
    for (const label of ['连接名称', '模型输入能力', '联网搜索']) assert(!(await page.getByLabel(label, { exact: true }).isVisible()), 'Advanced fields start collapsed');
    assert.equal(await page.getByLabel('可选模型列表').count(), 0, 'No redundant model list on the basic screen');
    await layout(); await capture('model-settings-simple');
    await page.getByLabel('模型 ID', { exact: true }).fill('new-model');
    await page.getByRole('button', { name: '管理模型', exact: true }).click();
    await page.getByLabel('可选模型列表').fill('research-model\nanother-model\nanother-model');
    await page.getByRole('button', { name: '管理模型', exact: true }).click();
    assert.equal(await page.getByLabel('模型 ID', { exact: true }).inputValue(), 'new-model');
    await page.locator('.model-options > summary').click();
    assert.equal(await page.getByLabel('联网搜索', { exact: true }).inputValue(), 'deepseek');
    await page.getByLabel('连接名称', { exact: true }).fill('研究连接');
    await page.locator('.model-form-body').evaluate(element => element.scrollTop = element.scrollHeight);
    await layout(); await capture('model-settings-advanced');
    await page.locator('.model-options > summary').click();
    for (const [width, height] of [[1200, 850], [1000, 760], [850, 700]]) {
      await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setSize(...size), [width, height]);
      await page.locator('.model-form-body').evaluate(element => element.scrollTop = 0);
      await layout();
    }
    await capture('model-settings-compact');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await page.getByRole('dialog', { name: '工作台设置' }).waitFor({ state: 'hidden' });
    const saved = JSON.parse(fs.readFileSync(path.join(config, 'config.json'), 'utf8')).model_connections[0];
    assert.equal(saved.name, '研究连接'); assert.equal(saved.model, 'new-model');
    assert.deepEqual(saved.models, ['research-model', 'another-model', 'new-model']);
    assert.deepEqual(saved.model_reasoning['research-model'], profile);
    assert.deepEqual(saved.model_inputs['research-model'], ['text', 'image']);
    assert.deepEqual(saved.native_search, { backend: 'deepseek', model: '', baseUrl: '' });
    assert.deepEqual(requests, [], 'Layout and saving must not call a model or provider');
    assert.deepEqual(errors, []);
    console.log('PASS: compact model settings, aligned controls at four sizes, persistent footer, hidden options preserve models/reasoning/search and save without API calls');
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
