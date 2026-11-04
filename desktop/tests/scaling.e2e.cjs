const { _electron: electron } = require('playwright');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-scaling-ui-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const dir of [data, config, home]) fs.mkdirSync(dir);
fs.writeFileSync(path.join(config, 'config.json'), '{}');
const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, HOME: home, USERPROFILE: home, XULOOM_TEST_HEADLESS: '1', XULOOM_SKILLS_PATHS: '' };
for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','ANTHROPIC_API_KEY','OPENAI_BASE_URL','ANTHROPIC_BASE_URL']) delete env[key];
let app, page; const errors = [];
(async () => { try {
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on('pageerror', e => errors.push(e.message));
  await page.getByRole('button', { name: 'Scaling 蜂群', exact: true }).click();
  const panel = page.getByRole('dialog', { name: 'Scaling 蜂群工作台' });
  await panel.getByRole('heading', { name: '开启大规模探索' }).waitFor();
  assert.match(await panel.innerText(), /大量模型请求/);
  assert.match(await panel.innerText(), /实验阶段.*开发测试版/);
  assert.equal(fs.existsSync(path.join(data, 'scaling', 'index.sqlite3')), false, 'opening the usage warning starts no service/model');
  await panel.getByRole('button', { name: '了解用量，进入配置' }).click();
  await panel.getByRole('heading', { name: '配置探索集群' }).waitFor();
  await panel.getByRole('button',{name:'添加本机',exact:true}).click();
  assert.equal(await panel.getByLabel('Scaling 执行位置').inputValue(),'本机（无需 SSH）');
  assert.equal(await panel.getByLabel('Scaling SSH 连接',{exact:true}).count(),0);
  await panel.getByLabel('Scaling 智能体上限',{exact:true}).fill('100');
  await panel.getByRole('button',{name:'保存节点',exact:true}).click();
  const localNode=panel.locator('.scaling-node').filter({hasText:'本机'});
  await localNode.waitFor();
  assert.match(await localNode.innerText(),/worker 上限 100.*2 个组长/);
  assert(await panel.getByRole('button',{name:'添加本机',exact:true}).isDisabled());
  await panel.getByRole('button', { name: '配置 SSH', exact: true }).click();
  await panel.getByRole('button', { name: '新增 SSH 连接', exact: true }).click();
  await panel.getByLabel('SSH 连接 ID', { exact: true }).fill('lab');
  await panel.getByLabel('SSH 连接名称', { exact: true }).fill('Local test host');
  await panel.getByLabel('SSH 主机', { exact: true }).fill('127.0.0.1');
  await panel.getByLabel('SSH 远端目录', { exact: true }).fill('/tmp/work');
  await panel.getByRole('button', { name: '保存 SSH 连接', exact: true }).click();
  await panel.getByRole('status').filter({ hasText: 'SSH 连接已保存' }).waitFor();
  await panel.getByRole('button', { name: '刷新 SSH 与节点列表' }).click();
  await panel.getByRole('button', { name: '添加节点', exact: true }).click();
  await panel.getByLabel('Scaling 节点名称', { exact: true }).fill('lab-node');
  await panel.getByLabel('Scaling SSH 连接', { exact: true }).selectOption('lab');
  await panel.getByLabel('Scaling 智能体上限', { exact: true }).fill('1000');
  assert.equal(await panel.getByLabel('Scaling API 来源').inputValue(), 'inherit');
  assert.equal(await panel.getByLabel('Scaling 分组宽度').inputValue(), '50');
  await panel.getByRole('button', { name: '保存节点', exact: true }).click();
  await panel.locator('.scaling-node').filter({ hasText: 'lab-node' }).waitFor();
  const saved = JSON.parse(fs.readFileSync(path.join(config, 'config.json'), 'utf8'));
  const savedLocal=saved.scaling_nodes.find(n=>n.location==='local'),savedSSH=saved.scaling_nodes.find(n=>n.id==='lab-node');
  assert.equal(savedLocal.max_agents,100);assert.equal(savedLocal.ssh,null);
  assert.equal(savedSSH.max_agents, 1000); assert.equal(savedSSH.api_source, 'inherit'); assert.equal(savedSSH.group_width, 50);
  await panel.getByRole('button', { name: '配置 SSH', exact: true }).click();
  await panel.locator('.scaling-node').filter({hasText:'lab-node'}).locator('input[type=checkbox]').check();
  await panel.getByLabel('Scaling 探索目标').fill('Compare independent evidence');
  const start = panel.getByRole('button', { name: '启动 Scaling', exact: true });
  assert(await start.isDisabled(), 'new runs require high-usage acknowledgment');
  // Deterministic presentation fixture only. Real encrypted SSH/native SSE is
  // covered separately by test_scaling_ssh.py; no real host/model is contacted.
  await app.evaluate(({ ipcMain }) => {
    const original = ipcMain._invokeHandlers.get('workbench:invoke');
    global.__scalingCalls = [];
    const messages=[];
    const run = { id: 'fixture-run', goal: 'Compare independent evidence', status: 'running', target: 1000, round: 1, counts: { running: 1, completed: 2 }, nodes: [], health: [{ id: 'fixture-run_lab-node', status: 'connected', running: 1, resources: { suggested: 4 } }] };
    ipcMain.removeHandler('workbench:invoke');
    ipcMain.handle('workbench:invoke', async (event, method, params) => {
      if (!['scaling.create','scaling.status','scaling.tasks','scaling.result','scaling.children','scaling.child_result','scaling.questions','scaling.message','scaling.messages','scaling.pause','scaling.resume','scaling.cancel'].includes(method)) return original(event, method, params);
      global.__scalingCalls.push({ method, params });
      if (method === 'scaling.pause') run.status = 'paused';
      if (method === 'scaling.resume') run.status = 'running';
      if (method === 'scaling.cancel') run.status = 'cancelled';
      if (method === 'scaling.questions') return [];
      if (method === 'scaling.messages') return JSON.parse(JSON.stringify(messages));
      if (method === 'scaling.message') {
        const message={id:params.request_id,text:params.text,status:'applied',reply:'已调整为验证方向',applied_round:2,nodes:{}};
        messages.push(message); run.goal='验证方向';run.round=2; return message;
      }
      if (method === 'scaling.children') return [{id:'child-fixture', kind:'explore', label:'Nested evidence', status:'completed', group:'group-one'}];
      if (method === 'scaling.child_result') return {id:'child-fixture',kind:'explore',label:'Nested evidence',status:'completed',session_id:'nested-history-id',receipt:{text:'Nested original evidence',persisted:true}};
      if (method === 'scaling.result') return { id: 'work-fixture', kind:'explore', label:'Independent evidence A', status:'completed', node:'lab-node', session_id:'native-history-id', cwd:'/owned/run/work', receipt:{persisted:true,text:'Original full evidence body'} };
      if (method === 'scaling.tasks') return [{id:'leader-fixture',kind:'leader',label:'SSH node primary',node:'lab-node',status:'running',child_run_id:'child-run'}, { id: 'work-fixture', kind: 'explore', group: 'group-A', label: 'Independent evidence A', status: 'completed', node: 'lab-node', session_id: 'native-history-id', cwd: '/owned/run/work', receipt: { persisted: true, text: 'Original full evidence body' } }];
      return JSON.parse(JSON.stringify(run));
    });
  });
  await panel.getByLabel('我了解此运行可能产生大量 API 用量，并同意按所选节点容量探索').check();
  assert(await start.isEnabled()); await start.click();
  await panel.getByLabel('给蜂群主 agent 发送消息').fill('请改成验证方向');
  await panel.getByRole('button',{name:'发送给主 agent',exact:true}).click();
  await panel.getByText('已调整为验证方向',{exact:true}).waitFor();
  await panel.getByRole('heading',{name:'验证方向',exact:true}).waitFor();
  await panel.getByText('新方向已应用 · 第 2 轮',{exact:true}).waitFor();
  await panel.getByRole('button', { name: /Independent evidence A/ }).click();
  await panel.getByText('Original full evidence body', { exact: true }).waitFor();
  assert.match(await panel.innerText(), /native-history-id/);
  await panel.getByRole('button', { name: /SSH node primary/ }).click();
  await panel.getByRole('button', { name: /Nested evidence/ }).click();
  await panel.getByText('Nested original evidence', {exact:true}).waitFor();
  assert.match(await panel.innerText(), /nested-history-id/);
  await panel.getByRole('button', { name: '暂停派发', exact: true }).click();
  await panel.getByText('已停止新派发；已开始的工具和模型请求可完成。取消运行才会向执行器发送停止请求。', { exact: true }).waitFor();
  await panel.getByRole('button', { name: '恢复', exact: true }).click();
  const calls = await app.evaluate(() => global.__scalingCalls);
  const creation = calls.find(c => c.method === 'scaling.create');
  assert.equal(calls.filter(c=>c.method==='scaling.message').length,1);
  assert.equal(calls.find(c=>c.method==='scaling.message').params.text,'请改成验证方向');
  assert.equal(creation.params.accept_high_usage, true); assert.match(creation.params.request_id, /^[a-f0-9-]+$/);
  assert.deepEqual(creation.params.nodes, ['lab-node']);
  for (const width of [1100, 850]) {
    await app.evaluate(({ BrowserWindow }, width) => BrowserWindow.getAllWindows()[0].setSize(width, 900), width);
    await page.waitForTimeout(100);
    assert(await panel.locator('.scaling-content').evaluate(e => e.scrollWidth <= e.clientWidth + 2), 'workbench must stay within its scroll viewport');
  }
  fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  fs.writeFileSync(path.join(root, 'test-results/scaling-workbench.png'), Buffer.from(png, 'base64'));
  await panel.getByRole('button', { name: '返回会话', exact: true }).click();
  assert.equal(await page.getByRole('dialog', { name: 'Scaling 蜂群工作台' }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('Scaling UI: warning, SSH/node persistence, usage gate, shared RPC, run/evidence, pause/resume and narrow layout passed');
} finally {
  if (app) await app.close();
  // Explicit authenticated idle shutdown of this test fixture's service.
  const endpoint = path.join(data, 'scaling', 'control-endpoint.json');
  if (fs.existsSync(endpoint)) {
    const python = process.env.XULOOM_PYTHON || path.join(root, '../.venv-mcp/Scripts/python.exe');
    await promisify(execFile)(python, ['-c', "import asyncio,json,sys;from xuloom.scaling.daemon import call;asyncio.run(call(json.load(open(sys.argv[1])), 'service.stop'))", endpoint], { cwd: path.join(root, '..'), env, windowsHide: true });
  }
} })().catch(e => { console.error(e); process.exitCode = 1; });
