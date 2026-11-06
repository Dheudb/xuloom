const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-scaling-loading-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const dir of [data, config, home]) fs.mkdirSync(dir);
fs.writeFileSync(path.join(config, 'config.json'), '{}');
const env = { ...process.env, XULOOM_DATA_DIR:data, XULOOM_USER_DATA:data, XULOOM_CONFIG_DIR:config, HOME:home, USERPROFILE:home, XULOOM_TEST_HEADLESS:'1' };
for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','ANTHROPIC_API_KEY']) delete env[key];
let app; const errors=[];
(async()=>{try {
  app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
  const page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',e=>errors.push(e.message));
  await app.evaluate(({ipcMain})=>{
    const original=ipcMain._invokeHandlers.get('workbench:invoke');
    global.scalingLoading={calls:[],config:null,list:null};
    ipcMain.removeHandler('workbench:invoke');
    ipcMain.handle('workbench:invoke',(event,method,params)=>{
      const g=global.scalingLoading;g.calls.push(method);
      if(method==='scaling.config')return new Promise((resolve,reject)=>{
        g.config=async(fail=false)=>{g.config=null;if(fail)reject(new Error('OWNED_CONFIG_FAILURE'));else try{resolve(await original(event,method,params));}catch(e){reject(e);}};
      });
      // Hold only history transport; real config/save still exercise the installed backend.
      if(method==='scaling.list')return new Promise((resolve,reject)=>{g.list=(fail=false)=>{g.list=null;if(fail)reject(new Error('OWNED_HISTORY_FAILURE'));else resolve([]);};});
      return original(event,method,params);
    });
  });
  async function open(){
    await page.getByRole('button',{name:'Scaling 蜂群',exact:true}).click();
    const panel=page.getByRole('dialog',{name:'Scaling 蜂群工作台'});
    await panel.getByRole('button',{name:'了解用量，进入配置'}).click();
    await panel.getByText('正在读取节点配置…',{exact:true}).waitFor();
    return panel;
  }
  async function release(kind,fail=false){await app.evaluate(async(_,v)=>{
    const fn=global.scalingLoading[v.kind];if(typeof fn!=='function')throw new Error('No pending '+v.kind);await fn(v.fail);
  },{kind,fail});}
  let panel=await open();
  const addLocal=()=>panel.getByRole('button',{name:'添加本机',exact:true});
  const addSSH=()=>panel.getByRole('button',{name:'添加节点',exact:true});
  const save=()=>panel.getByRole('button',{name:'保存节点',exact:true});
  const cancel=()=>panel.locator('.scaling-form').getByRole('button',{name:'取消',exact:true});
  assert(await addLocal().isEnabled());assert(await addSSH().isEnabled());
  await addLocal().click();await panel.getByLabel('Scaling 智能体上限',{exact:true}).fill('51');
  assert(await save().isDisabled(),'Unread config must not be overwritten');
  await cancel().click();await addSSH().click();await panel.getByLabel('Scaling 节点名称').fill('draft-node');
  await release('config');await panel.getByText('正在读取节点配置…',{exact:true}).waitFor({state:'hidden'});
  assert.equal(await panel.getByLabel('Scaling 节点名称').inputValue(),'draft-node','Config loading preserves drafts');
  await cancel().click();await addLocal().click();await panel.getByLabel('Scaling 智能体上限',{exact:true}).fill('51');
  assert(await save().isEnabled(),'Slow history must not disable saving loaded config');await save().click();
  await panel.locator('.scaling-node').filter({hasText:'本机'}).waitFor();
  assert.equal(JSON.parse(fs.readFileSync(path.join(config,'config.json'),'utf8')).scaling_nodes[0].max_agents,51);
  assert.equal(fs.existsSync(path.join(data,'scaling')),false,'Editing settings starts no controller or model');
  await panel.getByText('正在读取运行记录…',{exact:true}).waitFor();
  await addSSH().click();await panel.getByLabel('Scaling 节点名称').fill('retained-draft');
  await release('list',true);await panel.getByRole('alert').filter({hasText:'运行记录读取失败'}).waitFor();
  assert.equal(await panel.getByLabel('Scaling 节点名称').inputValue(),'retained-draft');
  assert(await addSSH().isEnabled());await cancel().click();
  await panel.getByRole('button',{name:'返回会话',exact:true}).click();
  panel=await open();await addLocal().click();await release('config',true);await release('list');
  await panel.getByRole('alert').filter({hasText:'节点配置读取失败'}).waitFor();assert(await save().isDisabled());
  await panel.getByRole('button',{name:'刷新 SSH 与节点列表',exact:true}).click();
  await panel.getByText('正在读取节点配置…',{exact:true}).waitFor();await release('config');await release('list');
  await panel.getByRole('alert').filter({hasText:'同名节点已存在'}).waitFor();assert(await save().isDisabled());
  assert.equal(JSON.parse(fs.readFileSync(path.join(config,'config.json'),'utf8')).scaling_nodes[0].max_agents,51,'An early add cannot replace an existing node');
  assert.equal(fs.existsSync(path.join(data,'scaling')),false);
  const calls=await app.evaluate(()=>global.scalingLoading.calls);
  assert.equal(calls.filter(x=>x==='scaling.save').length,1);assert(!calls.includes('scaling.create'));
  assert.deepEqual(errors,[]);
  console.log('PASS: immediate local/SSH forms, delayed config guards, draft preservation, save before history, load failure/retry, no node overwrite or controller/model startup.');
}finally{if(app)await app.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
