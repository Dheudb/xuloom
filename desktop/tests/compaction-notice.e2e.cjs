const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-auto-compact-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), project = path.join(fixture, 'project');
for (const directory of [data, config, home, project]) fs.mkdirSync(directory);
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: project }));
const errors = [];
let app, page, summaryMode = 'ready', pending = [];
function summary(res) { res.writeHead(200, { 'Content-Type':'application/json' }).end(JSON.stringify({ choices:[{ message:{content:'Verified evidence and sources retained; continue only the latest authorized task.'} }], usage:{prompt_tokens:3000,completion_tokens:25} })); }
const server = http.createServer((req, res) => {
  let raw = ''; req.on('data', part => raw += part); req.on('end', () => {
    const body = JSON.parse(raw);
    if (!body.stream) {
      if (!JSON.stringify(body.messages).includes('Conversation to summarize:')) return res.writeHead(200, { 'Content-Type':'application/json' }).end(JSON.stringify({choices:[{message:{content:'[]'}}]}));
      if (summaryMode === 'hold') { pending.push(res); return; }
      if (summaryMode === 'fail') return res.writeHead(400, { 'Content-Type':'application/json' }).end(JSON.stringify({error:{message:'Owned summary endpoint refused',type:'invalid_request_error'}}));
      return summary(res);
    }
    const text = JSON.stringify([...body.messages].reverse().find(item => item.role === 'user')?.content || '');
    const seed = text.includes('TRIGGER_') ? undefined : [...text.matchAll(/SEED_(\d)/g)].at(-1)?.[1];
    const answer = seed !== undefined ? 'evidence '.repeat(1800) + '\nSEED_RESULT_' + seed : 'COMPACTION_NOTICE_DONE';
    res.writeHead(200, { 'Content-Type':'text/event-stream' });
    for (const [delta, finish_reason] of [[{content:answer},null],[{},'stop']]) res.write('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason}]})+'\n\n');
    res.end('data: [DONE]\n\n');
  });
});
async function launch() {
  const env = {...process.env, XULOOM_TEST_HEADLESS:'1', XULOOM_USER_DATA:data, XULOOM_DATA_DIR:data, XULOOM_CONFIG_DIR:config, XULOOM_SKILLS_PATHS:'', HOME:home, USERPROFILE:home};
  for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER']) delete env[key];
  app = await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE ? [] : [root],env});
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on('pageerror', error => errors.push(error.stack));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
}
async function send(text) { await page.getByRole('textbox',{name:'任务输入'}).fill(text); await page.getByRole('button',{name:'发送任务',exact:true}).click(); }
async function seed() {
  summaryMode = 'ready'; pending = [];
  await page.locator('.new-task').click(); await page.locator('.session-item.active').waitFor();
  for (let i=0;i<4;i++) {
    await send('SEED_'+i);
    await page.locator('.message.assistant').last().getByText(new RegExp('SEED_RESULT_'+i)).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
  }
}
const latest = () => page.locator('.message.assistant').last();
async function capture() {
  fs.mkdirSync(path.join(root,'test-results'),{recursive:true});
  const png = await app.evaluate(async ({BrowserWindow}) => {
    const web=BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined,{stayHidden:true,stayAwake:true}); await new Promise(resolve=>setTimeout(resolve,100));
    return (await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');
  });
  fs.writeFileSync(path.join(root,'test-results/compaction-notice.png'),Buffer.from(png,'base64'));
}
(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  fs.writeFileSync(path.join(config,'config.json'),JSON.stringify({model_provider:'openai',active_model_connection:'owned',model_connections:[{
    id:'owned',name:'Owned local service',provider:'openai',model:'fixture',models:['fixture'],api_key:'fixture',base_url:`http://127.0.0.1:${server.address().port}/v1`,
    model_context:{fixture:{window:12000,reserve:4000,keepRecent:1024,backend:'summary'}}
  }]}));
  try {
    await launch(); await seed(); summaryMode='hold'; await send('TRIGGER_COMPRESS');
    await latest().locator('[data-compaction-status="running"]').waitFor();
    assert(await latest().getByText('正在压缩上下文…',{exact:true}).isVisible());
    for(let i=0;i<100&&!pending.length;i++) await page.waitForTimeout(30);
    assert(pending.length); await capture();
    summaryMode='ready'; for(const res of pending.splice(0)) summary(res);
    await latest().getByText('COMPACTION_NOTICE_DONE',{exact:true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    assert.equal(await latest().getByText('上下文已自动压缩',{exact:true}).count(),1);
    assert.equal(await latest().locator('details.turn-process').count(),0,'A compaction-only trace must not require expanding tool details');
    await app.close(); app=null; await launch();
    await latest().getByText('上下文已自动压缩',{exact:true}).waitFor();
    assert.equal(await latest().locator('[data-compaction-status="running"]').count(),0);
    await seed(); summaryMode='fail'; await send('TRIGGER_FAILURE');
    await latest().getByText('上下文压缩失败，原始记录已保留',{exact:true}).waitFor();
    assert.equal(await latest().getByText('上下文已自动压缩',{exact:true}).count(),0);
    await page.waitForFunction(() => !document.querySelector('.send-button.stop'));
    await seed(); summaryMode='hold'; await send('TRIGGER_CANCEL');
    await latest().locator('[data-compaction-status="running"]').waitFor();
    await page.getByRole('button',{name:'停止任务',exact:true}).click();
    await latest().getByText('上下文压缩已取消',{exact:true}).waitFor();
    assert.equal(await latest().getByText('上下文已自动压缩',{exact:true}).count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: real automatic compaction shows a live inline icon/label, settles once after persistence, survives restart without tools, and distinguishes failure/cancellation; local service only');
  } finally { if(app) await app.close().catch(()=>{}); server.closeAllConnections(); server.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
