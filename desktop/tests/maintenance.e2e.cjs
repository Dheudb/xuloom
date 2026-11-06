const { _electron: electron } = require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const { atomicJSON }=require('../electron/catalog.cjs');
const root=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-maintenance-'));
const data=path.join(fixture,'data'),config=path.join(fixture,'config'),home=path.join(fixture,'home');
for(const directory of [data,config,home])fs.mkdirSync(directory);
atomicJSON(path.join(data,'workbench.json'),{projects:[]});
let app,calls=0;const errors=[];
const server=http.createServer((req,res)=>{req.resume();req.on('end',()=>{
  calls++;res.writeHead(401,{'Content-Type':'application/json'}).end(JSON.stringify({error:{message:'PRIVATE-RESPONSE-SECRET',type:'invalid_api_key'}}));
});});
(async()=>{
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    atomicJSON(path.join(config,'config.json'),{model_provider:'openai',openai_model:'fixture',openai_api_key:'fixture',openai_base_url:`http://127.0.0.1:${server.address().port}/v1`});
    const env={...process.env,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,XULOOM_CONFIG_DIR:config,HOME:home,USERPROFILE:home,XULOOM_TEST_HEADLESS:'1'};
    for(const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER'])delete env[key];
    app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env,timeout:30000});
    const page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',error=>errors.push(error.message));
    await page.waitForFunction(()=>document.querySelector('.new-task')?.disabled===false);
    await page.locator('.new-task').click();await page.locator('.session-item.active').waitFor();
    await page.getByRole('textbox',{name:'任务输入'}).fill('AUTH_FAILURE');await page.getByRole('button',{name:'发送任务',exact:true}).click();
    const alert=page.locator('.session-error');await alert.waitFor();assert((await alert.innerText()).includes('401'));
    assert(!(await page.locator('body').innerText()).includes('PRIVATE-RESPONSE-SECRET'));assert.equal(calls,1,'Authentication failures do not blindly retry');
    await page.getByRole('button',{name:'检查模型设置',exact:true}).click();await page.getByRole('dialog',{name:'工作台设置',exact:true}).waitFor();
    await page.getByRole('tab',{name:'工作区',exact:true}).click();await page.locator('.build-details > summary').click();
    await page.getByText(/版本：0\.1\.33/).waitFor();
    const build=await page.evaluate(()=>window.xuloom.invoke('app.build-info'));
    if(process.env.XULOOM_TEST_EXECUTABLE){assert(build.buildId);assert(build.sourceCommit);assert.equal(build.dirty,false);assert.equal(build.validation.present,true);assert.equal(build.validation.realProvidersTested,false);}
    else assert.equal(build.validation.present,false,'Development mode must not claim packaged validation');
    assert.equal(calls,1,'Version and validation reads never request a model');
    const png=await app.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64'));
    fs.writeFileSync(path.join(root,'build','maintenance-settings.png'),Buffer.from(png,'base64'));
    assert.deepEqual(errors,[]);console.log('PASS: actionable authentication failure, response-body redaction, model settings entry, honest build/provenance/validation display; no extra model requests.');
  }finally{if(app)await app.close().catch(()=>{});server.closeAllConnections();server.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
