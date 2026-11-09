// Real hidden desktop + loopback provider: each chat owns its model/effort.
const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-chat-models-'));
const data = path.join(fixture,'data'), config = path.join(fixture,'config'), home = path.join(fixture,'home');
for (const dir of [data,config,home]) fs.mkdirSync(dir);
let app, page, pending; const requests = [], errors = [];
function reply(res,text) {
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  res.end('data: '+JSON.stringify({choices:[{index:0,delta:{content:text},finish_reason:null}]})+'\n\n'+
    'data: '+JSON.stringify({choices:[{index:0,delta:{},finish_reason:'stop'}]})+'\n\ndata: [DONE]\n\n');
}
const server = http.createServer((req,res)=>{
  let raw=''; req.on('data',part=>raw+=part); req.on('end',()=>{
    const body=JSON.parse(raw); requests.push(body);
    if (JSON.stringify(body.messages).includes('HOLD_CURRENT_TASK')) {pending=res;return;}
    reply(res,`REPLY_${body.model}_${body.reasoning_effort}`);
  });
});
async function launch() {
  const env={...process.env,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,XULOOM_CONFIG_DIR:config,
    USERPROFILE:home,HOME:home,XULOOM_SKILLS_PATHS:'',XULOOM_TEST_HEADLESS:'1',PYTHONIOENCODING:'utf-8'};
  for(const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER']) delete env[key];
  app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env,timeout:30000});
  page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',error=>errors.push(error.message));
  await page.waitForFunction(()=>document.querySelector('.new-task')?.disabled===false);
}
async function create() {
  const before=await page.locator('.session-item.active').count()?await page.locator('.session-item.active').getAttribute('data-session-id'):null;
  await page.locator('.new-task').click();
  await page.waitForFunction(before=>{const id=document.querySelector('.session-item.active')?.getAttribute('data-session-id');return id&&id!==before;},before);
  return page.locator('.session-item.active').getAttribute('data-session-id');
}
async function select(model) {
  await page.getByLabel('选择模型',{exact:true}).selectOption(JSON.stringify(['fixture',model]));
  await page.waitForFunction(()=>!document.querySelector('.model-picker').disabled);
}
async function send(text,expected) {
  await page.getByRole('textbox',{name:'任务输入'}).fill(text);
  await page.getByRole('button',{name:'发送任务',exact:true}).click();
  if(expected){await page.getByText(expected,{exact:true}).last().waitFor();await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));}
}
async function activate(id) {
  await page.locator(`.session-item[data-session-id="${id}"]`).click();
  await page.waitForFunction(id=>document.querySelector('.session-item.active')?.getAttribute('data-session-id')===id,id);
}
(async()=>{
  try {
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const profile={adapter:'openai',efforts:['low','high'],effort:'low',thinking:'default',canDisable:false};
    const cfg={active_model_connection:'fixture',model_provider:'openai',model_connections:[{id:'fixture',name:'Fixture',provider:'openai',
      model:'one',models:['one','two'],base_url:`http://127.0.0.1:${server.address().port}/v1`,api_key:'loopback-fixture',
      model_reasoning:{one:profile,two:profile}}]};
    const file=path.join(config,'config.json');fs.writeFileSync(file,JSON.stringify(cfg));const original=fs.readFileSync(file,'utf8');
    await launch();const first=await create();await select('two');await send('FIRST_TASK','REPLY_two_low');
    const second=await create();assert.equal(await page.getByLabel('选择模型',{exact:true}).inputValue(),JSON.stringify(['fixture','two']));
    await select('one');
    await page.getByLabel('推理强度',{exact:true}).selectOption('high');
    await page.waitForFunction(()=>!document.querySelector('.reasoning-picker').disabled);
    await page.getByLabel('访问权限',{exact:true}).selectOption('full');
    await page.getByLabel('协作档位',{exact:true}).selectOption('solo');
    await page.waitForFunction(()=>!document.querySelector('.collaboration-picker').disabled);
    await send('SECOND_TASK','REPLY_one_high');
    await activate(first);assert.equal(await page.getByLabel('选择模型',{exact:true}).inputValue(),JSON.stringify(['fixture','two']));
    assert.equal(await page.getByLabel('推理强度',{exact:true}).inputValue(),'low');
    await send('HOLD_CURRENT_TASK');await page.getByRole('button',{name:'停止任务',exact:true}).waitFor();
    await activate(second);await select('two');
    await page.getByLabel('推理强度',{exact:true}).selectOption('high');
    await page.waitForFunction(()=>!document.querySelector('.reasoning-picker').disabled);
    assert(pending,'First chat remains running while a different chat changes model');reply(pending,'HELD_TASK_DONE');pending=null;
    await activate(first);await page.getByText('HELD_TASK_DONE',{exact:true}).waitFor();
    await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));
    assert.equal(await page.getByLabel('推理强度',{exact:true}).inputValue(),'low');
    const catalogProjection=value=>value.model_connections.map(({id,model,models,model_reasoning})=>({id,model,models,model_reasoning}));
    assert.deepEqual(catalogProjection(JSON.parse(fs.readFileSync(file,'utf8'))),catalogProjection(JSON.parse(original)),'Preferences do not rewrite other models or per-chat profiles');
    await app.close();app=null;await launch();
    assert.equal(await page.getByLabel('选择模型',{exact:true}).inputValue(),JSON.stringify(['fixture','two']));
    assert.equal(await page.getByLabel('推理强度',{exact:true}).inputValue(),'low');
    await activate(second);assert.equal(await page.getByLabel('推理强度',{exact:true}).inputValue(),'high');
    await page.getByRole('button',{name:'设置与模型',exact:true}).click();
    await page.getByRole('dialog',{name:'工作台设置'}).waitFor();
    assert.equal(await page.getByLabel('模型 ID',{exact:true}).inputValue(),'two','Settings reflect this chat, not the global default');
    await page.getByRole('button',{name:'关闭设置',exact:true}).click();
    await send('AFTER_RESTART','REPLY_two_high');
    await page.getByLabel('协作档位',{exact:true}).selectOption('ultra');
    await page.waitForFunction(()=>!document.querySelector('.collaboration-picker').disabled);
    await create();
    assert.equal(await page.getByLabel('选择模型',{exact:true}).inputValue(),JSON.stringify(['fixture','two']));
    assert.equal(await page.getByLabel('推理强度',{exact:true}).inputValue(),'high');
    assert.equal(await page.getByLabel('访问权限',{exact:true}).inputValue(),'full');
    assert.equal(await page.getByLabel('协作档位',{exact:true}).inputValue(),'ultra');
    assert.deepEqual(requests.map(row=>[row.model,row.reasoning_effort]),[['two','low'],['one','high'],['two','low'],['two','high']]);
    const empty=await page.evaluate(session=>window.xuloom.invoke('config.delete',{session,connectionId:'fixture',confirm:true}),second);
    assert.equal(empty.hasKey,false);assert.equal(empty.model,'');assert.deepEqual(empty.connections,[]);
    const cleared=await page.evaluate(session=>window.xuloom.invoke('config.get',{session}),second);
    assert.equal(cleared.model,'','Deleting the current connection also clears this chat\'s binding');
    const unavailable=await page.evaluate(session=>window.xuloom.invoke('config.get',{session}),first);
    assert.equal(unavailable.hasKey,false,'A removed shared credential is not retained by another chat');
    assert.deepEqual(errors,[]);
    console.log('PASS: model/effort remain per conversation; latest explicit model/effort/permissions/collaboration seed new chats; old chats and catalogs stay unchanged; restart and actual loopback requests verified.');
  } finally {if(app)await app.close().catch(()=>{});server.closeAllConnections();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
