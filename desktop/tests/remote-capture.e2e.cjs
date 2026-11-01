const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),readline=require('node:readline');
const {resolveEnginePython}=require('../electron/runtime.cjs');
const root=path.resolve(__dirname,'..'),repository=path.dirname(root);
if(process.env.XULOOM_TEST_BROWSER_RUNTIME!=='1'){console.log('Remote capture UI skipped: optional Chromium fixture required');process.exit(0);}
const fixture=fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-remote-capture-'));
const python=resolveEnginePython({packaged:false,projectRoot:repository});
const service=spawn(python,['-u',path.join(repository,'tests/remote_workbench_fixture.py'),fixture],
  {cwd:repository,env:process.env,windowsHide:true,stdio:['pipe','pipe','pipe']});
let diagnostics='',app,page;service.stderr.on('data',chunk=>diagnostics+=chunk);
const ready=new Promise((resolve,reject)=>{
  readline.createInterface({input:service.stdout}).once('line',line=>{try{resolve(JSON.parse(line));}catch(error){reject(error);}});
  service.once('exit',code=>reject(new Error('Owned SSH fixture failed '+code+' '+diagnostics.slice(-2000))));
});
(async()=>{try{
  const info=await ready;
  fs.writeFileSync(path.join(info.remote,'page.html'),'<html><title>REMOTE_HTML</title><link rel="stylesheet" href="style.css"><body>Remote capture</body></html>');
  fs.writeFileSync(path.join(info.remote,'style.css'),'body{margin:0;background:#aabbcc}');
  const env={...process.env,HOME:path.join(fixture,'local-home'),USERPROFILE:path.join(fixture,'local-home'),XULOOM_CONFIG_DIR:info.config,
    XULOOM_DATA_DIR:path.join(fixture,'local-data'),XULOOM_USER_DATA:path.join(fixture,'local-data'),XULOOM_TEST_HEADLESS:'1',XULOOM_SKILLS_PATHS:''};
  for(const key of Object.keys(env))if(key.startsWith('OPENAI_')||key.startsWith('ANTHROPIC_')||['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','XULOOM_MODEL_PROVIDER'].includes(key))delete env[key];
  app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
  page=await app.firstWindow();page.setDefaultTimeout(45000);const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.waitForFunction(()=>!document.querySelector('.new-task')?.disabled);
  // Fail if any remote event tries to invoke the local Chromium renderer.
  await app.evaluate(({app,BrowserWindow},remote)=>{
    const load=process.getBuiltinModule('node:module').createRequire(app.getAppPath()+'/package.json');
    load('./electron/browser-capture.cjs').BrowserCaptures.prototype.capture=async()=>{throw new Error('LOCAL_RENDER_MUST_NOT_RUN');};
    const fs=process.getBuiltinModule('node:fs/promises');
    for(const name of ['readFile','realpath','stat']){const original=fs[name];fs[name]=function(file,...rest){if(String(file).startsWith(remote))throw new Error('LOCAL_REMOTE_FILE_READ_DENIED');return original.call(this,file,...rest);};}
  },info.remote);
  await page.getByRole('button',{name:'设置与模型',exact:true}).click();await page.getByRole('tab',{name:'SSH 远程',exact:true}).click();
  await page.getByRole('button',{name:'新建远程会话',exact:true}).click();await page.locator('.session-item.active[data-session-id^="ssh:"]').waitFor();
  await page.getByLabel('访问权限',{exact:true}).selectOption('full');
  await page.waitForFunction(()=>!document.querySelector('.permission-picker')?.disabled);
  for(const mode of ['FILE','URL']){
    await page.getByRole('textbox',{name:'任务输入',exact:true}).fill('REMOTE_CAPTURE_'+mode);
    await page.getByRole('button',{name:'发送任务',exact:true}).click();
    await page.getByText('REMOTE_CAPTURE_'+mode+'_VERIFIED',{exact:true}).last().waitFor();
    await page.getByRole('button',{name:'发送任务',exact:true}).waitFor();
  }
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().length),1);
  await page.locator('.turn-process').first().locator(':scope > summary').click();
  const image=page.locator('.tool-image-thumb').first();await image.scrollIntoViewIfNeeded();await image.locator('img').waitFor();
  await image.click();await page.getByRole('dialog',{name:'图片附件预览',exact:true}).waitFor();await page.keyboard.press('Escape');
  const png=await app.evaluate(async({BrowserWindow})=>{const web=BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined,{stayHidden:true,stayAwake:true});await new Promise(r=>setTimeout(r,100));
    return(await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');});
  fs.mkdirSync(path.join(root,'test-results'),{recursive:true});fs.writeFileSync(path.join(root,'test-results/remote-capture.png'),Buffer.from(png,'base64'));
  assert.deepEqual(errors,[]);
  const wire=fs.readFileSync(path.join(fixture,'wire.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
  const web=wire.filter(row=>row.web_headers);assert(web.length);assert(web.every(row=>!row.web_headers.Authorization&&!row.web_headers.Cookie));
  await app.close();app=null;
  // Reopen the exact remote session, then open the saved image snapshot.
  app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
  page=await app.firstWindow();page.setDefaultTimeout(45000);
  await page.locator('.session-item.active[data-session-id^="ssh:"]').waitFor();
  await page.locator('.turn-process').first().locator(':scope > summary').click();
  const restored=page.locator('.tool-image-thumb').first();await restored.scrollIntoViewIfNeeded();await restored.locator('img').waitFor();
}finally{
  if(app)await app.close();service.stdin.end('{"action":"stop"}\n');
  if(service.exitCode===null)await new Promise((resolve,reject)=>{const timer=setTimeout(()=>{service.kill();reject(new Error('Owned fixture shutdown timed out'));},30000);
    service.once('exit',code=>{clearTimeout(timer);code===0?resolve():reject(new Error('Fixture failed '+code+' '+diagnostics.slice(-1800)));});});
}
console.log('Remote screenshot App passed: actual execution-host Chromium, CSS pixels sent to native model input, URL rendering, no local browser/files/cookies/key, image enlargement and remote history reopening');
})().catch(error=>{console.error(error);process.exitCode=1;});
