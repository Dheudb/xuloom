const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),readline=require('node:readline');
const {resolveEnginePython}=require('../electron/runtime.cjs'),{atomicJSON}=require('../electron/catalog.cjs');
const root=path.resolve(__dirname,'..'),repository=path.dirname(root);
const backend=process.env.XULOOM_TEST_DISCOVERY_BACKEND||'pi',program=process.env['XULOOM_TEST_'+backend.toUpperCase()+'_PROGRAM'];
if(!program){console.log('External discovery UI skipped: installed '+backend+' required');process.exit(0);}
const bootstrap=fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-discovery-bootstrap-'));
const serviceEnv={...process.env,HOME:bootstrap,USERPROFILE:bootstrap,XULOOM_DATA_DIR:path.join(bootstrap,'data'),XULOOM_CONFIG_DIR:path.join(bootstrap,'config'),
  XULOOM_SKILLS_PATHS:'',OPENAI_API_KEY:'fixture-only',ANTHROPIC_API_KEY:'fixture-only',OPENAI_BASE_URL:'http://127.0.0.1:1/v1',ANTHROPIC_BASE_URL:'http://127.0.0.1:1',HTTP_PROXY:'',HTTPS_PROXY:'',ALL_PROXY:'',NO_PROXY:'*'};
const service=spawn(resolveEnginePython({packaged:false,projectRoot:repository}),['-u',path.join(repository,'tests/external_discovery_fixture.py')],
  {cwd:repository,env:serviceEnv,windowsHide:true,stdio:['pipe','pipe','pipe']});
let app,page,info,diagnostics='';service.stderr.on('data',chunk=>diagnostics+=chunk);
const ready=new Promise((resolve,reject)=>{
  readline.createInterface({input:service.stdout}).once('line',line=>{try{resolve(JSON.parse(line));}catch(error){reject(error);}});
  service.once('exit',code=>reject(new Error('Fixture exited '+code+' '+diagnostics.slice(-2000))));
});
(async()=>{try{
  info=await ready;
  atomicJSON(path.join(info.data,'workbench.json'),{projects:[info.project],activeConversation:{project:info.project,resumeId:info.sessionId,scope:'project'}});
  const env={...process.env,HOME:info.home,USERPROFILE:info.home,XULOOM_DATA_DIR:info.data,XULOOM_USER_DATA:info.data,
    XULOOM_CONFIG_DIR:info.config,XULOOM_TEST_HEADLESS:'1',XULOOM_SKILLS_PATHS:''};
  for(const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_MODEL','OPENAI_BASE_URL','XULOOM_MODEL_PROVIDER'])delete env[key];
  for(const key of Object.keys(env))if(key.startsWith('ANTHROPIC_'))delete env[key];
  Object.assign(env,{XULOOM_MODEL_PROVIDER:'anthropic',ANTHROPIC_MODEL:'claude-sonnet-4-5-20250929',ANTHROPIC_BASE_URL:process.env.XULOOM_TEST_AMBIENT_BASE||info.baseUrl,
    ANTHROPIC_API_KEY:'fixture-only',XULOOM_NETWORK_MODE:'direct',HTTP_PROXY:'',HTTPS_PROXY:'',ALL_PROXY:'',NO_PROXY:'*'});
  app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
  page=await app.firstWindow();page.setDefaultTimeout(45000);const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.locator('.session-item.active').waitFor();
  const config=await page.evaluate(()=>window.xuloom.invoke('config.get',{}));
  assert.equal(config.baseUrl,info.baseUrl);assert.equal(config.provider,'anthropic');
  await page.getByLabel('访问权限',{exact:true}).selectOption('full');
  await page.waitForFunction(()=>!document.querySelector('.permission-picker')?.disabled);
  await page.getByRole('textbox',{name:'任务输入',exact:true}).fill('Continue the discovered worker in App');
  await page.getByRole('button',{name:'发送任务',exact:true}).click();
  await page.getByText('APP_CATALOG_COMPLETE',{exact:true}).last().waitFor();
  await page.getByRole('button',{name:'发送任务',exact:true}).waitFor();
  if(await page.getByRole('button',{name:'展开能力面板',exact:true}).count())await page.getByRole('button',{name:'展开能力面板',exact:true}).click();
  await page.getByRole('button',{name:'代理',exact:true}).click();
  const workers=await page.evaluate(()=>window.xuloom.invoke('agents.list',{session:document.querySelector('.session-item.active').dataset.sessionId}));
  const worker=workers.agents.find(worker=>worker.agent_id===info.workerId);
  assert.equal(worker.turn,2);assert.equal(worker.status,'completed',worker.result);
  assert.equal(worker.backend,backend);
  if(backend==='dsh'){
    await page.locator('.worker-model').filter({hasText:'DSH'}).waitFor();
    await page.getByRole('button',{name:'设置与模型',exact:true}).click();
    await page.getByRole('tab',{name:'Subagents',exact:true}).click();
    await page.getByRole('button',{name:'新建 Subagent',exact:true}).click();
    await page.getByLabel('代理后端',{exact:true}).selectOption('dsh');
    await page.getByLabel('代理程序路径',{exact:true}).fill(program);
    await page.getByLabel('扩展名称',{exact:true}).fill('dsh-from-settings');
    await page.getByLabel('扩展描述',{exact:true}).fill('Review independent evidence');
    await page.getByLabel('代理工具',{exact:true}).fill('Read');
    await page.getByLabel('扩展指令',{exact:true}).fill('Read the supplied evidence.');
    await page.evaluate(()=>document.activeElement?.blur());
    await page.getByLabel('代理后端',{exact:true}).evaluate(element=>element.scrollIntoView({block:'center'}));
    const image=await app.evaluate(async({BrowserWindow})=>{
      const web=BrowserWindow.getAllWindows()[0].webContents;
      await web.capturePage(undefined,{stayHidden:true,stayAwake:true});
      await new Promise(resolve=>setTimeout(resolve,100));
      return(await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');
    });
    fs.writeFileSync(path.join(root,'test-results/dsh-settings.png'),Buffer.from(image,'base64'));
    await page.getByRole('button',{name:'保存 Subagent',exact:true}).click();
    await page.getByRole('status').filter({hasText:'Subagent 已保存并加载'}).waitFor();
    await page.getByRole('button',{name:'关闭设置',exact:true}).click();
    const caps=await page.evaluate(()=>window.xuloom.invoke('capabilities.list',{session:document.querySelector('.session-item.active').dataset.sessionId}));
    const role=caps.agents.find(role=>role.name==='dsh-from-settings');assert(role.valid);assert.equal(role.backend,'dsh');
  }
  assert.deepEqual(errors,[]);
}catch(error){
  console.error('App assertion failed:',error);
  if(page)console.error('App alerts:',await page.locator('.session-error').allTextContents());
  throw error;
}finally{
  if(app)await app.close();
  service.stdin.end('\n');
  if(service.exitCode===null)await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>{service.kill();reject(new Error('Owned fixture timed out'));},60000);
    service.once('exit',code=>{clearTimeout(timeout);code===0?resolve():reject(new Error('Fixture failed '+code+' '+diagnostics.slice(-3000)));});
  });
}
console.log('External discovery App + formal CLI passed: actual '+backend+' tools, same worker after restarts, one discovery and three native list receipts, unselected tools stay hidden');
})().catch(error=>{console.error(error);process.exitCode=1;});
