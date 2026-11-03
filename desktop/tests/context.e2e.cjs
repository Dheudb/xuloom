const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-context-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), project = path.join(fixture, 'project');
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), 'resources/python/python.exe') : process.env.XULOOM_PYTHON || 'python';
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: project, python }));
const errors = [], requests = [];
let app, page;
let holdSummary = false, summaryEntered = false;
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', data => raw += data);
  req.on('end', () => {
    try {
      const body = JSON.parse(raw); requests.push(body);
      if (!body.stream) {
        const summarizing = JSON.stringify(body.messages).includes('Conversation to summarize:');
        if (holdSummary && summarizing) { summaryEntered = true; return; }
        const content = summarizing ? 'Verified evidence retained. User prohibits publication. No tools executed. Continue only the authorized question.' : '[]';
        return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content } }], usage: { prompt_tokens: 2000, completion_tokens: 24 } }));
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const [delta, finish_reason] of [[{ content: 'CONTEXT_APP_PASS' }, null], [{}, 'stop']])
        res.write('data: ' + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
      res.end('data: [DONE]\n\n');
    } catch (error) { errors.push(error.stack); res.writeHead(500).end('Owned fixture failed'); }
  });
});
async function launch() {
  const env = { ...process.env, XULOOM_TEST_HEADLESS: '1', XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: '', HOME: home, USERPROFILE: home, XULOOM_NETWORK_MODE: 'direct' };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER', 'PYTHONHOME', 'PYTHONPATH']) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on('pageerror', error => errors.push(error.stack));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
}
async function openContext() {
  await page.getByRole('button', { name: '添加工具与模式', exact: true }).click();
  await page.getByRole('menuitem', { name: '上下文 查看用量或手动压缩' }).click();
  await page.getByRole('dialog', { name: '上下文用量' }).waitFor();
  await page.waitForFunction(() => document.querySelector('.context-total') || document.querySelector('.context-dialog [role="alert"]'));
  if (await page.locator('.context-dialog [role="alert"]').count()) throw new Error(await page.locator('.context-dialog [role="alert"]').innerText());
  await page.locator('.context-total').waitFor();
}
async function snapshot() {
  return page.evaluate(() => window.xuloom.invoke('context.get', { session: document.querySelector('.session-item.active').getAttribute('data-session-id') }));
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ active_model_connection: 'owned', model_provider: 'openai', model_connections: [{
    id: 'owned', name: 'Owned fixture', provider: 'openai', model: 'fixture', models: ['fixture'], api_key: 'owned-key', base_url: `http://127.0.0.1:${server.address().port}/v1`,
    model_context: { fixture: { window: 32768, keepRecent: 1024, reserve: 4096, backend: 'summary' } }
  }] }));
  try {
    await launch(); await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    await page.getByRole('textbox', { name: '任务输入' }).fill('Do not publish. Supplied evidence: ' + 'source '.repeat(7500));
    await page.getByRole('button', { name: '发送任务', exact: true }).click();
    await page.getByText('CONTEXT_APP_PASS', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    await openContext(); assert.equal(requests.length, 1, 'Inspecting must not call a model');
    const initial = await snapshot();
    assert.equal(initial.window, 32768);
    await page.getByLabel('压缩保留重点', { exact: true }).fill('Keep evidence and authorization');
    await page.getByRole('button', { name: '压缩上下文', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.context-dialog')?.textContent.includes('上次压缩：') || document.querySelector('.context-dialog [role="alert"]'));
    if (await page.locator('.context-dialog [role="alert"]').count()) throw new Error(await page.locator('.context-dialog [role="alert"]').innerText());
    await page.getByText(/上次压缩：/).waitFor();
    const report = await snapshot();
    assert(report.last.after < report.last.before); assert(report.last.after <= report.inputBudget);
    assert.equal(report.auxiliary.compaction.calls, 1); assert.equal(report.auxiliary.compaction.input_tokens, 2000);
    assert(fs.existsSync(report.last.archive)); assert(requests[1].messages[0].content.includes('Keep evidence and authorization'));
    const bounds = await page.locator('.context-dialog').evaluate(element => ({ right: element.getBoundingClientRect().right, width: innerWidth, overflow: element.scrollWidth > element.clientWidth + 1 }));
    assert(!bounds.overflow && bounds.right <= bounds.width);
    fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
    const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
    fs.writeFileSync(path.join(root, 'test-results/context-compaction.png'), Buffer.from(png, 'base64'));
    await page.getByRole('button', { name: '关闭上下文', exact: true }).click();
    await app.close(); app = null; await launch(); await openContext();
    assert.equal((await snapshot()).last.after, report.last.after);
    assert.equal(requests.length, 2, 'Restart and inspection must not infer or compact again');
    await page.getByRole('button', { name: '关闭上下文', exact: true }).click();
    await page.getByRole('button', { name: '设置与模型', exact: true }).click();
    await page.locator('.model-options > summary').click();
    assert.equal(await page.getByLabel('模型上下文窗口', { exact: true }).inputValue(), '32768');
    await page.getByLabel('模型上下文窗口', { exact: true }).fill('49152');
    assert.equal(await page.getByLabel('API Key', { exact: true }).getAttribute('placeholder'), '••••••••');
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    await page.getByRole('dialog', { name: '工作台设置' }).waitFor({ state: 'hidden' });
    assert.equal(JSON.parse(fs.readFileSync(path.join(config, 'config.json'))).model_connections[0].model_context.fixture.window, 49152);
    await page.getByRole('textbox', { name: '任务输入' }).fill('New authorized evidence: ' + 'source '.repeat(3000));
    await page.getByRole('button', { name: '发送任务', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.message.assistant').length >= 2 && !document.querySelector('.send-button.stop'));
    await openContext(); const beforeCancel = await snapshot(); holdSummary = true;
    await page.getByRole('button', { name: '压缩上下文', exact: true }).click();
    for (let attempt = 0; attempt < 100 && !summaryEntered; attempt++) await page.waitForTimeout(50);
    assert(summaryEntered, 'The owned summary request must start before cancellation');
    await page.getByRole('button', { name: '停止压缩', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '上下文压缩已取消' }).waitFor();
    assert.equal((await snapshot()).estimatedTokens, beforeCancel.estimatedTokens, 'Cancellation must retain the uncompressed conversation');
    assert.deepEqual(errors, []);
    console.log('PASS: hidden App context menu, exact per-model window, manual focus, durable summary/archive, auxiliary usage, restart without inference, compact settings and retained saved key; owned local service only');
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
