const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const { SessionCatalog, atomicJSON } = require('../electron/catalog.cjs');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-process-layout-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config');
for (const dir of [data, config]) fs.mkdirSync(dir);
let settings = { projects: [] }, app;
async function capture(name) {
  const png = await app.evaluate(async ({ BrowserWindow }) =>
    (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  fs.writeFileSync(path.join(root, 'build', name + '.png'), Buffer.from(png, 'base64'));
}
const catalog = new SessionCatalog(() => settings, value => settings = value, data);
const row = catalog.createStandalone(fixture);
catalog.update(row, { title: '紧凑的执行过程' });
const tools = Array.from({ length: 9 }, (_, index) => ({ kind: 'tool', tool: {
  id: 'tool-' + index, name: ['web_search_tool', 'web_fetch_tool', 'command_tool'][index % 3],
  status: index % 2 ? 'error' : 'completed', startedAt: 1000, finishedAt: 19000,
  input: index % 3 === 0 ? { query: 'A long scientific literature query about reliable retrieval and evidence '.repeat(4) } : index % 3 === 1 ? { url: 'https://example.org/long/path/to/paper' } : { command: 'python analyse.py --input dataset.csv' },
  result: index % 2 ? 'HTTP 403: website refused access; no content was read.' : 'Stored evidence'
} }));
const steps = [{ kind: 'text', text: '先检索公开资料，再核对可访问的来源。' },
  ...tools.slice(0, 4), { kind: 'text', text: ' \n\n ' },
  { kind: 'notice', notice: { status: 'completed', message: '工具参数校验失败，正在请求模型修正。', attempt: 1, limit: 2 } },
  ...tools.slice(4), { kind: 'text', text: '检索已结束，无法访问的来源会明确标注。' }];
atomicJSON(catalog.file(row.project, row.sessionId), { messages: [{ role: 'user', _uuid: 'q', content: '查询资料并核对来源。' }, { role: 'assistant', content: '结果已整理。' }],
  metadata: { turn_traces: [{ id: 'layout', userMessageId: 'q', status: 'completed', elapsedMs: 145000, steps, answer: '结果已整理。' }] } });
settings.activeConversation = { project: row.project, resumeId: row.sessionId, scope: 'standalone' };
atomicJSON(path.join(data, 'workbench.json'), settings);
atomicJSON(path.join(config, 'config.json'), { model_provider: 'openai', openai_model: 'fixture', openai_api_key: 'fixture', openai_base_url: 'http://127.0.0.1:9/v1' });
async function checkRows(page) {
  const measured = await page.locator('.turn-process-content').evaluate(content => {
    const rows = [...content.querySelectorAll('.turn-tool > summary, .turn-retry > summary')];
    return rows.map(element => {
      const box = element.getBoundingClientRect(), icon = element.querySelector('svg').getBoundingClientRect(), style = getComputedStyle(element);
      return { top: box.top, height: box.height, left: icon.left, line: style.lineHeight, color: style.color, width: box.width, overflow: element.scrollWidth - element.clientWidth };
    });
  });
  assert.equal(measured.length, 10);
  for (let index = 0; index < measured.length; index++) {
    assert.equal(measured[index].height, 28, 'Success, failure and Retry use the same row height');
    assert.equal(measured[index].left, measured[0].left, 'All icons share one left edge');
    assert.equal(measured[index].color, 'rgb(130, 134, 139)');
    assert(measured[index].overflow <= 1, 'Long URLs/queries must not push controls outside the row');
    if (index) assert(Math.abs(measured[index].top - measured[index - 1].top - 28) < .1, 'No status-dependent or blank-text gaps');
  }
}
(async () => {
  try {
    const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
      USERPROFILE: path.join(fixture, 'home'), HOME: path.join(fixture, 'home'), XULOOM_SKILLS_PATHS: '', XULOOM_TEST_HEADLESS: '1' };
    for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER']) delete env[key];
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
    const page = await app.firstWindow(), errors = [];
    page.on('pageerror', error => errors.push(error.message)); page.setDefaultTimeout(15000);
    await page.getByText('结果已整理。', { exact: true }).waitFor();
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 980));
    const trace = page.locator('.turn-process'); await trace.locator(':scope > summary').click();
    assert.equal(await trace.locator('.turn-commentary').count(), 2);
    await checkRows(page);
    await capture('process-layout-wide');
    const failed = trace.locator('.turn-tool[data-tool-status="error"]').first();
    await failed.locator('summary').focus(); await page.keyboard.press('Enter');
    assert(await failed.locator('.turn-tool-detail').isVisible());
    assert((await failed.locator('.turn-tool-detail').innerText()).includes('403'));
    await failed.locator('summary').click(); await checkRows(page);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1000, 800));
    await page.waitForFunction(() => innerWidth <= 1100); await checkRows(page);
    await capture('process-layout-narrow');
    const retry = trace.locator('.turn-retry'); await retry.locator('summary').click();
    assert(await retry.locator('.turn-retry-detail').isVisible());
    assert((await retry.innerText()).includes('工具参数校验失败'));
    assert.deepEqual(errors, []);
    console.log('PASS: 28px success/error/retry rows, aligned icons, neutral tone, blank-step filtering, keyboard details and no long-query overflow at two widths; no model requests.');
  } finally { if (app) await app.close().catch(() => {}); }
})().catch(error => { console.error(error); process.exitCode = 1; });
