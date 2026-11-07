const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const { SessionCatalog, atomicJSON } = require('../electron/catalog.cjs');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-reference-list-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), project = path.join(fixture, 'project');
for (const dir of [data, config, project]) fs.mkdirSync(dir);
let settings = { projects: [project] };
const catalog = new SessionCatalog(() => settings, value => { settings = value; }, data), row = catalog.create(project);
const sources = Array.from({length:13}, (_,i) => `[${i+1}] Author ${i+1}. [Paper ${i+1}: closed-loop electrochemical optimization](https://example.org/papers/${i+1}?version=1#abstract)`).join(' ');
const answer = '正文引用 [1] 和 [2] 保持原样。\n\n---\n\n' + sources;
const file = catalog.file(project, row.sessionId);
atomicJSON(file, {messages:[{role:'user',content:'查看文献',_uuid:'question'},{role:'assistant',content:answer}],
  metadata:{turn_traces:[{id:'reference-trace',userMessageId:'question',status:'completed',elapsedMs:1000,steps:[],answer,files:[]}]}});
const original = fs.readFileSync(file);
settings.activeConversation = {project,resumeId:row.sessionId,scope:'project'};
atomicJSON(path.join(data, 'workbench.json'), settings);
atomicJSON(path.join(config, 'config.json'), {model_provider:'openai',openai_model:'',openai_api_key:'',openai_base_url:'http://127.0.0.1:9/v1'});
let app;
const errors = [];
async function launch() {
  const env = {...process.env, XULOOM_DATA_DIR:data, XULOOM_USER_DATA:data, XULOOM_CONFIG_DIR:config,
    HOME:path.join(fixture,'home'), USERPROFILE:path.join(fixture,'home'), XULOOM_TEST_HEADLESS:'1'};
  for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER']) delete env[key];
  app = await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE ? [] : [root],env});
  const page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on('pageerror', e=>errors.push(e.message));
  await page.getByRole('list',{name:'参考来源'}).waitFor();
  return page;
}
async function main() {
  try {
    let page = await launch();
    await app.evaluate(({shell,clipboard}) => { globalThis.referenceOpens=[]; globalThis.referenceCopies=[];
      shell.openExternal=async url=>{globalThis.referenceOpens.push(url);}; clipboard.writeText=text=>{globalThis.referenceCopies.push(text);}; });
    const list = page.getByRole('list',{name:'参考来源'}), items = list.getByRole('listitem');
    assert.equal(await items.count(),13);
    const bounds = await items.evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect(); return {top:r.top,bottom:r.bottom,scroll:n.scrollWidth,width:n.clientWidth};}));
    for (let i=1;i<bounds.length;i++) assert(bounds[i].top>=bounds[i-1].bottom+8,'Sources must be separate, evenly spaced rows');
    assert(bounds.every(r=>r.scroll<=r.width+1),'Long references should wrap without horizontal overflow');
    await page.getByText('正文引用 [1] 和 [2] 保持原样。',{exact:true}).waitFor();
    await list.getByRole('link',{name:'Paper 3: closed-loop electrochemical optimization',exact:true}).click();
    assert.deepEqual(await app.evaluate(()=>globalThis.referenceOpens),['https://example.org/papers/3?version=1#abstract']);
    await page.getByRole('button',{name:'复制回复',exact:true}).click();
    assert.deepEqual(await app.evaluate(()=>globalThis.referenceCopies),[answer],'Copy must retain original Markdown');
    await page.setViewportSize({width:1050,height:850});
    assert.equal(await list.evaluate(n=>n.scrollWidth<=n.clientWidth+1),true);
    await list.scrollIntoViewIfNeeded();
    const png = await app.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64'));
    fs.writeFileSync(path.join(root,'build/reference-list.png'),Buffer.from(png,'base64'));
    await app.close(); app=undefined;
    page=await launch(); assert.equal(await page.getByRole('list',{name:'参考来源'}).getByRole('listitem').count(),13);
    assert.deepEqual(fs.readFileSync(file),original,'History must remain unchanged');
    assert.deepEqual(errors,[]);
    console.log('PASS: 13 compact sources become readable numbered rows; links, wrapping, exact copy and restart preserve the original history. No model requests.');
  } finally { if(app) await app.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
