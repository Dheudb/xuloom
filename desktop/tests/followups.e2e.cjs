const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-followup-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), project = path.join(fixture, 'project');
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
const python = process.env.XULOOM_TEST_EXECUTABLE ? path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE), 'resources/python/python.exe') : process.env.XULOOM_PYTHON || 'python';
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: project, python }));
const errors = [], requests = [];
let app, page;
const server = http.createServer((req, res) => {
  let text = ''; req.on('data', data => text += data);
  req.on('end', () => {
    try {
      const body = JSON.parse(text);
      if (!body.stream) return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { content: '[]' } }] }));
      requests.push(body);
      assert(JSON.stringify(body.messages).includes('marker_observed_not_task_verified'));
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const [delta, finish_reason] of [[{ content: 'FOLLOWUP_APP_PASS' }, null], [{}, 'stop']])
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
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ model_provider: 'openai', openai_model: 'fixture', openai_api_key: 'owned-fixture', openai_base_url: `http://127.0.0.1:${server.address().port}/v1` }));
  try {
    await launch(); await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    const workspace = JSON.parse(fs.readFileSync(path.join(data, 'workbench.json'))).activeConversation.project;
    const session = await page.locator('.session-item.active').getAttribute('data-session-id');
    const params = { session, prompt: 'Inspect the experiment', trigger: { kind: 'file', path: 'experiment.done' }, request_id: 'owned-experiment' };
    const first = await page.evaluate(params => window.xuloom.invoke('schedule.add', params), params);
    const duplicate = await page.evaluate(params => window.xuloom.invoke('schedule.add', params), params);
    assert.equal(first.id, duplicate.id);
    await page.waitForTimeout(5500); assert.equal(requests.length, 0, 'Waiting must not call the model');
    fs.writeFileSync(path.join(workspace, 'experiment.done'), 'READY');
    await page.getByText('FOLLOWUP_APP_PASS', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    for (let attempt = 0; attempt < 100; attempt++) {
      if (JSON.parse(fs.readFileSync(path.join(data, 'schedules.json'))).items[first.id].status === 'completed') break;
      await page.waitForTimeout(100);
    }
    const row = JSON.parse(fs.readFileSync(path.join(data, 'schedules.json'))).items[first.id];
    assert.equal(row.status, 'completed'); assert.equal(row.last_delivery.input_status, 'processed'); assert.equal(row.last_delivery.acceptance, 'execution_only');
    assert.equal(requests.length, 1);
    await app.close(); app = null; await launch();
    await page.getByText('FOLLOWUP_APP_PASS', { exact: true }).waitFor();
    await page.waitForTimeout(5500); assert.equal(requests.length, 1, 'Restart must not repeat delivery');
    assert.deepEqual(errors, []);
    console.log('PASS: hidden App follows an owned marker through the actual scheduler, waits without model traffic, deduplicates, displays a result and restores without replay; local service only');
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
