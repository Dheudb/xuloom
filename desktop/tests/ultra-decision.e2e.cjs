// Actual desktop and CLI against loopback SSE only; no configured user services.
const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const assert = require('node:assert/strict'), { spawn } = require('node:child_process');
const { atomicJSON } = require('../electron/catalog.cjs');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-ultra-decision-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const dir of [data, config, home]) fs.mkdirSync(dir);
atomicJSON(path.join(data, 'workbench.json'), { projects: [] });
let app, page, calls = 0, simpleCalls = 0, sequence = 0;
const failures = [], children = [];
function reply(res, text, name, args) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  const chunk = (delta, finish_reason = null) => res.write('data: ' + JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
  if (name) {
    const specs = Array.isArray(name) ? name : [{name,args}];
    chunk({ tool_calls: specs.map((spec,index)=>({index,id:'fixture_'+ ++sequence,type:'function',function:{name:spec.name,arguments:JSON.stringify(spec.args)}})) });
    chunk({}, 'tool_calls');
  } else { chunk({ content: text }); chunk({}, 'stop'); }
  res.end('data: [DONE]\n\n');
}
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', part => raw += part); req.on('end', () => {
    try {
      const body = JSON.parse(raw), history = JSON.stringify(body.messages);
      if (!body.stream) return res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ choices: [{ message: { role: 'assistant', content: 'memory' } }] }));
      const human = body.messages.filter(m => m.role === 'user' && typeof m.content === 'string');
      if (human.some(m => m.content.startsWith('CHILD_'))) {
        children.push(res); return;
      }
      const task = human.findLast(m => m.content.startsWith('ULTRA_'))?.content || '';
      if (task.startsWith('ULTRA_SIMPLE')) {
        simpleCalls++;
        assert.equal(body.tool_choice,'required');
        const names = body.tools.map(t=>t.function.name);
        assert(!names.includes('collaboration_decision'));
        assert.deepEqual([...names].sort(),['ultra_answer','ultra_prepare']);
        return reply(res, null, 'ultra_answer', {answer:'SIMPLE_PASS'});
      }
      if (task.startsWith('ULTRA_COMPLEX')) {
        switch (++calls) {
          case 1:
            assert.equal(body.tool_choice,'required');
            assert(body.tools.some(t=>t.function.name==='ultra_prepare'));
            return reply(res, null, 'ultra_prepare', {task_type:'multi_path',main_task:'Organize the main note',evidence:'provided',rationale:'Independent supplied sources',workers:[
              {description:'第一来源',prompt:'CHILD_FIRST: check source one'},
              {description:'第二来源',prompt:'CHILD_SECOND: check source two'}]});
          case 2:
            assert(body.tools.some(t => t.function.name === 'spawn_agent'));
            return reply(res, null, 'file_write_tool', { file_path: 'main-evidence.txt', content: 'PARENT_WORK' });
          case 3:
            res.writeHead(200, {'Content-Type':'text/event-stream'});
            res.write('data: '+JSON.stringify({choices:[{index:0,delta:{content:'PROVISIONAL_MUST_WAIT'},finish_reason:null}]})+'\n\n');
            setTimeout(()=>res.end('data: '+JSON.stringify({choices:[{index:0,delta:{},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n'), 1200);
            return;
          case 4:
            assert(history.includes('EVIDENCE_FIRST') && history.includes('EVIDENCE_SECOND'));
            return reply(res, 'COMPLEX_SYNTHESIS_PASS');
          default: throw new Error('Unexpected extra main model round');
        }
      }
      throw new Error('Unexpected fixture request');
    } catch (error) { failures.push(error.stack); res.writeHead(500).end('fixture failed'); }
  });
});
function environment() {
  const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
    USERPROFILE: home, HOME: home, XULOOM_SKILLS_PATHS: '', XULOOM_TEST_HEADLESS: '1', XULOOM_NETWORK_MODE: 'direct', PYTHONIOENCODING: 'utf-8' };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER', 'XULOOM_COLLABORATION_MODE']) delete env[key];
  return env;
}
async function launch() {
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env: environment(), timeout: 30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on('pageerror', error => failures.push(error.message));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
}
async function send(text) {
  await page.getByRole('textbox', { name: '任务输入' }).fill(text);
  await page.getByRole('button', { name: '发送任务', exact: true }).click();
}
async function capture() {
  const png = await app.evaluate(async ({ BrowserWindow }) => (await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'));
  fs.writeFileSync(path.join(root, 'build', 'ultra-decision.png'), Buffer.from(png, 'base64'));
}
async function cli() {
  const env = environment(); env.PYTHONPATH = process.env.XULOOM_TEST_SITE || path.resolve(root, '..');
  const python = process.env.XULOOM_PYTHON || path.join(root, 'build', 'python', 'python.exe');
  const child = spawn(python, ['-m', 'xuloom', 'exec', 'ULTRA_SIMPLE_CLI', '--collaboration', 'ultra', '--json', '--cwd', fixture], { env, cwd: fixture, windowsHide: true });
  let stdout = '', stderr = ''; child.stdout.on('data', part => stdout += part); child.stderr.on('data', part => stderr += part);
  const timeout = setTimeout(() => child.kill(), 30000);
  try {
    const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
    assert.equal(code, 0, stderr + stdout);
    const events = stdout.trim().split(/\r?\n/).map(JSON.parse), finals = events.filter(e => e.type === 'final');
    assert.equal(finals.length, 1); assert.equal(finals[0].result.text, 'SIMPLE_PASS');
    assert(!events.some(e => e.type === 'collaboration_decision'));
    assert(!events.some(e => e.type === 'text_delta' && e.text.includes('"decision"')));
  } finally { clearTimeout(timeout); }
}
(async () => {
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    atomicJSON(path.join(config, 'config.json'), { model_provider: 'openai', openai_model: 'fixture', openai_api_key: 'fixture', openai_base_url: `http://127.0.0.1:${server.address().port}/v1` });
    await launch(); await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
    await page.getByLabel('访问权限', { exact: true }).selectOption('full');
    await page.getByLabel('协作档位', { exact: true }).selectOption('ultra');
    await page.waitForFunction(() => !document.querySelector('.collaboration-picker').disabled);
    await send('ULTRA_SIMPLE: 2+2?'); await page.getByText('SIMPLE_PASS', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    assert.equal(simpleCalls, 1); assert.equal(children.length, 0);
    assert.equal(await page.locator('.turn-retry:not(.turn-decision)').count(), 0, 'Simple conversation has no protocol retry');
    const originalId=await page.locator('.session-item.active').getAttribute('data-session-id');
    await send('ULTRA_COMPLEX: Compare two independent evidence sources, organize a main note, and synthesize.');
    await page.locator('.turn-commentary').filter({hasText:'Independent supplied sources'}).waitFor();
    assert.equal(await page.locator('.message-text').filter({hasText:'Independent supplied sources'}).count(),0,'Model-authored approach belongs in the process');
    await page.locator('.turn-commentary').filter({hasText:'PROVISIONAL_MUST_WAIT'}).waitFor();
    assert.equal(await page.locator('.message-text').filter({hasText:'PROVISIONAL_MUST_WAIT'}).count(),0,'Provisional output must stream inside process, never as the final answer');
    await page.getByText('等待 2 个子代理返回结果，全部结束后再汇总。', { exact: true }).waitFor();
    assert.equal(children.length, 2); assert.equal(calls, 3);
    if (await page.getByRole('button', {name:'展开能力面板',exact:true}).count())
      await page.getByRole('button', {name:'展开能力面板',exact:true}).click();
    await page.getByRole('button', {name:'代理',exact:true}).click();
    await page.getByRole('button', {name:'查看完整对话历史',exact:true}).first().click();
    const liveHistory = page.getByRole('dialog', {name:'子代理完整历史',exact:true});
    await liveHistory.getByText('已到当前快照末尾',{exact:true}).waitFor();
    assert((await liveHistory.innerText()).includes('CHILD_'), 'A running child checkpoint is readable before its first model result');
    assert(!(await liveHistory.innerText()).includes('Agent receipt is missing'));
    await liveHistory.getByLabel('关闭子代理历史',{exact:true}).click();
    await page.locator('.new-task').click();
    await page.waitForFunction(id=>document.querySelector('.session-item.active')?.getAttribute('data-session-id')!==id,originalId);
    await page.getByLabel('协作档位',{exact:true}).selectOption('ultra');
    await page.waitForFunction(()=>!document.querySelector('.collaboration-picker').disabled);
    await send('ULTRA_SIMPLE_OTHER: answer in another conversation');
    await page.getByText('SIMPLE_PASS',{exact:true}).waitFor();await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));
    assert.equal(children.length,2);assert.equal(calls,3,'Switching and asking elsewhere never restarts background workers');
    await page.locator(`.session-item[data-session-id="${originalId}"]`).click();
    await page.getByText('等待 2 个子代理返回结果，全部结束后再汇总。',{exact:true}).waitFor();
    assert.equal(await page.getByText('COMPLEX_SYNTHESIS_PASS', { exact: true }).count(), 0);
    assert.equal(await page.locator('.send-button.stop').count(), 1);
    const trace = page.locator('.turn-process').last();
    assert.equal(await page.locator('.turn-decision').count(),0);
    const workspaces = path.join(data, 'workspaces', 'default');
    const notes = fs.readdirSync(workspaces).map(entry => path.join(workspaces, entry, 'main-evidence.txt')).filter(file => fs.existsSync(file));
    assert.equal(notes.length, 1); assert.equal(fs.readFileSync(notes[0], 'utf8'), 'PARENT_WORK');
    reply(children[0], 'EVIDENCE_FIRST');
    await page.getByText('等待 1 个子代理返回结果，全部结束后再汇总。', { exact: true }).waitFor();
    assert.equal(calls, 3); reply(children[1], 'EVIDENCE_SECOND');
    await page.getByText('COMPLEX_SYNTHESIS_PASS', { exact: true }).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    await page.getByRole('button', {name:'查看完整对话历史',exact:true}).first().click();
    const finishedHistory = page.getByRole('dialog', {name:'子代理完整历史',exact:true});
    await finishedHistory.getByText('已到当前快照末尾',{exact:true}).waitFor();
    assert((await finishedHistory.innerText()).includes('EVIDENCE_'), 'Completed receipt remains readable');
    await finishedHistory.getByLabel('关闭子代理历史',{exact:true}).click();
    await app.close(); app = null; await launch();
    await page.getByText('COMPLEX_SYNTHESIS_PASS', { exact: true }).waitFor();
    await page.locator('.turn-process').last().locator(':scope > summary').click();
    assert((await page.locator('.turn-process').last().innerText()).includes('PROVISIONAL_MUST_WAIT'),'Draft remains in the saved process after restart');
    assert.equal(await page.locator('.turn-process').last().locator('.turn-commentary').filter({hasText:'Independent supplied sources'}).count(),1,'Accepted approach survives restart once');
    assert.equal(await page.locator('.message-text').filter({hasText:'PROVISIONAL_MUST_WAIT'}).count(),0);
    assert.equal(await page.locator('.turn-process').last().locator('.turn-tool').count(),4);
    assert(!(await page.locator('.conversation').innerText()).includes('"decision"'));
    await capture(); await app.close(); app = null;
    await cli(); assert.equal(simpleCalls, 3); assert.deepEqual(failures, []);
    console.log('PASS: desktop + CLI native Ultra assessment; simple conversation one request/zero workers; planned workers run alongside main work; wait-all synthesis and live history survive restart. Loopback models only.');
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
