const {_electron:electron}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),fixture=fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-steering-'));
const data=path.join(fixture,'data'),config=path.join(fixture,'config'),home=path.join(fixture,'home');
for(const dir of [data,config,home])fs.mkdirSync(dir);
const requests=[];let held,app,page;const errors=[];
const text=m=>typeof m.content==='string'?m.content:JSON.stringify(m.content);
const server=http.createServer((req,res)=>{let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{
 const body=JSON.parse(raw);requests.push(body);
 const user=body.messages.filter(m=>m.role==='user').map(text).join('\n');
 res.writeHead(200,{'Content-Type':'text/event-stream'});
 const chunk=(delta,finish=null)=>res.write('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason:finish}]})+'\n\n');
 if(!user.includes('CHANGE_A')&&!user.includes('CHANGE_C')){
   chunk({content:user.includes('RUN_TWO')?'PARTIAL_TWO':'PARTIAL_ONE'});
   held=()=>{chunk({},'stop');res.end('data: [DONE]\n\n');};
   const timer=setInterval(()=>res.write(': alive\n\n'),200);res.on('close',()=>clearInterval(timer));
 }else {chunk({content:user.includes('CHANGE_C')?'APPLIED_C':'APPLIED_A'});chunk({},'stop');res.end('data: [DONE]\n\n');}
});});
async function launch(){
 const env={...process.env,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,XULOOM_CONFIG_DIR:config,HOME:home,USERPROFILE:home,XULOOM_TEST_HEADLESS:'1'};
 for(const k of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','ANTHROPIC_API_KEY'])delete env[k];
 app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
 page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',e=>errors.push(e.message));
 await page.waitForFunction(()=>document.querySelector('.new-task')?.disabled===false);
 await page.evaluate(()=>{window.steerEvents=[];window.xuloom.onEvent(e=>{if(e.type.startsWith('user_input'))window.steerEvents.push(e);});});
}
const input=()=>page.getByRole('textbox',{name:'任务输入',exact:true});
async function send(value){await input().fill(value);await page.getByRole('button',{name:'发送任务',exact:true}).click();}
async function append(value){await input().fill(value);await page.getByRole('button',{name:'追加指令',exact:true}).click();}
(async()=>{try{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 fs.writeFileSync(path.join(config,'config.json'),JSON.stringify({model_provider:'openai',openai_model:'fixture',openai_api_key:'fixture',openai_base_url:`http://127.0.0.1:${server.address().port}/v1`}));
 await launch();await page.locator('.new-task').click();await send('RUN_ONE');await page.getByText('PARTIAL_ONE',{exact:true}).waitFor();
 assert(await input().isEnabled());
 await app.evaluate(({ipcMain})=>{
  const original=ipcMain._invokeHandlers.get('workbench:invoke');let drop=true;
  ipcMain.removeHandler('workbench:invoke');ipcMain.handle('workbench:invoke',async(e,m,p)=>{
   const value=await original(e,m,p);if(m==='prompt.append'&&drop){drop=false;throw new Error('OWNED_DROPPED_ACK');}return value;
  });
 });
 await append('CHANGE_A');await page.locator('.session-error').filter({hasText:'OWNED_DROPPED_ACK'}).waitFor();
 assert.equal(await input().inputValue(),'CHANGE_A');assert.equal(await page.locator('.pending-inputs > div').count(),1);
 assert(await page.getByRole('button',{name:'停止任务',exact:true}).isEnabled());
 await page.getByRole('button',{name:'追加指令',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('textarea[aria-label="任务输入"]').value==='');
 assert.equal(await page.locator('.pending-inputs > div').count(),1,'Lost acknowledgement retries deduplicate');
 await append('WITHDRAW_B');await page.locator('.pending-inputs > div').filter({hasText:'WITHDRAW_B'}).getByRole('button',{name:'取消补充要求'}).click();
 await page.waitForFunction(()=>document.querySelectorAll('.pending-inputs > div').length===1);
 assert.equal(requests.length,1,'Queueing/withdrawing never calls a router model');
 held();await page.getByText('APPLIED_A',{exact:true}).waitFor();await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));
 assert.equal(await page.locator('.message.user').count(),2);assert.equal(await page.locator('.message.assistant').count(),2);
 assert.equal(await page.locator('.pending-inputs').count(),0);assert.equal(requests.length,2);
 assert(!JSON.stringify(requests[1].messages).includes('WITHDRAW_B'));
 await app.close();await launch();await page.getByText('APPLIED_A',{exact:true}).waitFor();
 assert.equal(await page.locator('.message.user').count(),2);assert.equal(await page.locator('.message.assistant').count(),2);
 await page.locator('.new-task').click();await send('RUN_TWO');await page.getByText('PARTIAL_TWO',{exact:true}).waitFor();
 await append('CHANGE_C');await page.locator('.pending-inputs').filter({hasText:'CHANGE_C'}).waitFor();
 await page.getByRole('button',{name:'停止任务',exact:true}).click();await page.getByRole('button',{name:'继续执行',exact:true}).waitFor();
 await app.close();await launch();await page.locator('.pending-inputs').filter({hasText:'CHANGE_C'}).waitFor();
 await page.getByRole('button',{name:'继续执行',exact:true}).click();await page.getByText('APPLIED_C',{exact:true}).waitFor();
 await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));
 assert.equal(await page.getByText('PARTIAL_TWOPARTIAL_TWO',{exact:true}).count(),0,'Resume does not duplicate the prefix when applying saved input');
 assert.equal(await page.locator('.message.user').count(),2);assert.equal(await page.locator('.message.assistant').count(),2);
 assert.equal(requests.length,4);assert.deepEqual(errors,[]);
 fs.mkdirSync(path.join(root,'test-results'),{recursive:true});
 const png=await app.evaluate(async({BrowserWindow})=>(await BrowserWindow.getAllWindows()[0].webContents.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64'));
 fs.writeFileSync(path.join(root,'test-results/steering.png'),Buffer.from(png,'base64'));
 console.log('PASS: live input, durable queue, lost-ack deduplication, withdrawal, chronological replies, saved history, stop/restart/resume, no duplicate prefix or router request.');
}finally{if(app)await app.close();server.closeAllConnections();server.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
