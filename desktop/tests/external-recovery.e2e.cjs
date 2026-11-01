const {_electron: electron} = require('playwright');
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const {spawn} = require('node:child_process'), readline = require('node:readline');
const os = require('node:os');
const {resolveEnginePython} = require('../electron/runtime.cjs');
const {atomicJSON} = require('../electron/catalog.cjs');
const root = path.resolve(__dirname, '..'), repository = path.dirname(root);
if (!process.env.XULOOM_TEST_PI_PROGRAM) { console.log('External recovery UI skipped: set XULOOM_TEST_PI_PROGRAM to an installed Pi'); process.exit(0); }
const python = resolveEnginePython({packaged:false, projectRoot:repository});
const bootstrap = fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-external-ui-bootstrap-'));
const serviceEnv = {...process.env, HOME:bootstrap, USERPROFILE:bootstrap, XULOOM_DATA_DIR:path.join(bootstrap,'data'),
  XULOOM_CONFIG_DIR:path.join(bootstrap,'config'), XULOOM_SKILLS_PATHS:'', OPENAI_API_KEY:'fixture-only', ANTHROPIC_API_KEY:'fixture-only',
  OPENAI_BASE_URL:'http://127.0.0.1:1/v1', ANTHROPIC_BASE_URL:'http://127.0.0.1:1', HTTP_PROXY:'', HTTPS_PROXY:'', ALL_PROXY:'', NO_PROXY:'*'};
const service = spawn(python, ['-u', path.join(repository,'tests/external_recovery_fixture.py')], {cwd:repository, env:serviceEnv, windowsHide:true, stdio:['pipe','pipe','pipe']});
let diagnostics = '', app, page;
service.stderr.on('data', chunk => diagnostics += chunk);
const ready = new Promise((resolve,reject) => {
  readline.createInterface({input:service.stdout}).once('line', line => {try {resolve(JSON.parse(line));} catch(error) {reject(error);}});
  service.once('exit', code => reject(new Error('Fixture exited '+code+' '+diagnostics.slice(-3000))));
});
(async()=>{try {
  const info = await ready;
  atomicJSON(path.join(info.data,'workbench.json'), {projects:[info.project], activeConversation:{project:info.project,resumeId:info.sessionId,scope:'project'}});
  const env = {...process.env, HOME:info.home, USERPROFILE:info.home, XULOOM_DATA_DIR:info.data, XULOOM_USER_DATA:info.data,
    XULOOM_CONFIG_DIR:info.config, XULOOM_TEST_HEADLESS:'1', XULOOM_SKILLS_PATHS:''};
  for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_MODEL','OPENAI_BASE_URL','XULOOM_MODEL_PROVIDER']) delete env[key];
  const failures = [];
  async function open() {
    app = await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
    page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on('pageerror', error => failures.push(error.message));
    await page.locator('.session-item.active').waitFor();
    if (await page.getByRole('button',{name:'展开能力面板',exact:true}).count()) await page.getByRole('button',{name:'展开能力面板',exact:true}).click();
    await page.getByRole('button',{name:'代理',exact:true}).click();
  }
  async function verify() {
    const good = page.locator('.capability-card').filter({has:page.getByText('Confirmed external stop',{exact:true})});
    const unknown = page.locator('.capability-card').filter({has:page.getByText('Unconfirmed external stop',{exact:true})});
    await good.getByText('停止已确认，可在主会话继续此代理。',{exact:true}).waitFor();
    await unknown.getByText('停止结果尚未确认，请先核对已有产物，不会自动重放。',{exact:true}).waitFor();
    const layout = await good.locator('.worker-meta').evaluate(element=>{
      const label=element.querySelector('.worker-model'), status=element.querySelector('.worker-status');
      const range=document.createRange();range.selectNodeContents(status);
      return {lines:range.getClientRects().length,overflow:label.scrollWidth>label.clientWidth,title:label.title,ellipsis:getComputedStyle(label).textOverflow};
    });
    assert.equal(layout.lines,1);assert.equal(layout.overflow,true);assert.equal(layout.ellipsis,'ellipsis');assert(layout.title.length>20);
    const result = await page.evaluate(() => window.xuloom.invoke('agents.list',{session:document.querySelector('.session-item.active').dataset.sessionId}));
    assert.equal(result.agents.find(a=>a.agent_id===info.confirmedId).external_recovery.resumable,true);
    assert.equal(result.agents.find(a=>a.agent_id===info.uncertainId).external_recovery.resumable,false);
  }
  await open(); await verify();
  fs.mkdirSync(path.join(root,'test-results'),{recursive:true});
  const screenshot = await app.evaluate(async ({BrowserWindow})=>{
    const web=BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined,{stayHidden:true,stayAwake:true});
    return (await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');
  });
  fs.writeFileSync(path.join(root,'test-results/external-recovery.png'),Buffer.from(screenshot,'base64'));
  await app.close(); app = null;
  await open(); await verify();
  assert.deepEqual(failures,[]);
} finally {
  if (app) await app.close();
  service.stdin.end('\n');
  if (service.exitCode===null) await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>{service.kill(); reject(new Error('Owned fixture cleanup timed out'));},45000);
    service.once('exit', code=>{clearTimeout(timeout);code===0?resolve():reject(new Error('Fixture failed '+code+' '+diagnostics.slice(-3000)));});
  });
}
console.log('External recovery App + formal CLI passed: actual Pi stop/crash receipts, distinct recovery states, restart persistence and same-worker CLI continuation without model replay');
})().catch(error=>{console.error(error);process.exitCode=1;});
