const { _electron: electron } = require('playwright');
const fs=require('node:fs'), path=require('node:path'), os=require('node:os'), assert=require('node:assert/strict'), {spawnSync}=require('node:child_process');
const {atomicJSON}=require('../electron/catalog.cjs'), {historyRoot}=require('../electron/history-storage.cjs');
const root=path.resolve(__dirname,'..'), fixture=fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-cleanup-ui-'));
const data=path.join(fixture,'data'),home=path.join(fixture,'home'),config=path.join(fixture,'config'),project=path.join(fixture,'project');
for(const dir of [data,home,config,project])fs.mkdirSync(dir);
const env={...process.env,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,XULOOM_CONFIG_DIR:config,HOME:home,USERPROFILE:home,XULOOM_TEST_HEADLESS:'1',XULOOM_SKILLS_PATHS:''};
for(const name of Object.keys(env))if(name.startsWith('OPENAI_')||name.startsWith('ANTHROPIC_')||['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','XULOOM_MODEL_PROVIDER'].includes(name))delete env[name];
const python=process.env.XULOOM_TEST_EXECUTABLE?path.join(path.dirname(process.env.XULOOM_TEST_EXECUTABLE),'resources/python/python.exe'):path.join(root,'../.venv-mcp/Scripts/python.exe');
atomicJSON(path.join(config,'config.json'),{});
atomicJSON(path.join(historyRoot(project,data),'sessions/cleanup.json'),{messages:[],metadata:{}});
atomicJSON(path.join(data,'workbench.json'),{projects:[project],activeConversation:{project,resumeId:'cleanup',scope:'project'}});
const setup=`import ctypes,json,sys,uuid
from ctypes import wintypes
from xuloom.security.windows_sandbox import WindowsAPI
from xuloom.security.sandbox_cleanup import save
api=WindowsAPI(); identity=uuid.uuid4().hex; name='xuloom.sandbox.'+identity; sid=ctypes.c_void_p()
result=api.userenv.CreateAppContainerProfile(name,name,'Xuloom owned UI cleanup fixture',None,0,ctypes.byref(sid))
assert result>=0
text=wintypes.LPWSTR(); api.check(api.advapi.ConvertSidToStringSidW(sid,ctypes.byref(text)))
save(dict(id=identity,profile=name,sid=text.value,workspace=sys.argv[1],status='cleanup_failed',grants=[],traversed=[],failures=[dict(operation='delete_profile',error='owned fixture')]))
api.kernel.LocalFree(text);api.advapi.FreeSid(sid);print(identity)
`;
let app,page,identity;const errors=[];
(async()=>{try{
 const result=spawnSync(python,['-I','-B','-c',setup,project],{env,encoding:'utf8',windowsHide:true});
 assert.equal(result.status,0,result.stderr);identity=result.stdout.trim();assert.match(identity,/^[a-f0-9]{32}$/);
 app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
 page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',e=>errors.push(e.message));
 await page.locator('.session-item.active').waitFor();
 await page.getByRole('button',{name:'添加工具与模式',exact:true}).click();
 await page.getByRole('menuitem',{name:/^执行隔离 /}).click();
 const dialog=page.getByRole('dialog',{name:'执行隔离设置',exact:true});
 await dialog.getByText('上次隔离执行的权限清理尚未完成。执行结果保留，未自动重跑任务。',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'重试清理权限',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('[role="dialog"] [role="alert"]'));
 assert.equal(await dialog.getByRole('button',{name:'重试清理权限',exact:true}).count(),0);
 const record=JSON.parse(fs.readFileSync(path.join(data,'sandbox-cleanup/completed',identity+'.json'),'utf8'));
 assert.equal(record.status,'cleaned');assert.deepEqual(record.failures,[]);
 const damagedId=require('node:crypto').randomUUID().replaceAll('-',''),damaged=path.join(data,'sandbox-cleanup',damagedId+'.json');
 fs.writeFileSync(damaged,'{broken JSON');
 await dialog.getByRole('button',{name:'关闭执行隔离设置',exact:true}).click();
 await page.getByRole('button',{name:'添加工具与模式',exact:true}).click();
 await page.getByRole('menuitem',{name:/^执行隔离 /}).click();
 await page.getByText('清理记录损坏，无法确认残留状态。原文件已保留，请检查记录后再重试。',{exact:true}).waitFor();
 assert(await page.getByRole('button',{name:'重试清理权限',exact:true}).isDisabled());
 assert.equal(fs.readFileSync(damaged,'utf8'),'{broken JSON');
 assert.deepEqual(errors,[]);console.log('Sandbox cleanup UI passed: original backend record, visible warning, explicit recovery, actual owned profile deletion, durable clean receipt; no model calls');
}catch(error){if(page)console.error((await page.locator('body').innerText()).slice(-4500));throw error;}finally{
 if(app)await app.close();
 if(identity)spawnSync(python,['-I','-B','-c',"import sys;from xuloom.security.sandbox_cleanup import retry;retry(sys.argv[1],sys.argv[2])",project,identity],{env,windowsHide:true});
}})().catch(error=>{console.error(error);process.exitCode=1;});
