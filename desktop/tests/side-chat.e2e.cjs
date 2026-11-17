const { _electron: electron } = require("playwright");
const fs=require("node:fs"),path=require("node:path"),os=require("node:os"),http=require("node:http"),assert=require("node:assert/strict");
const {atomicJSON}=require("../electron/catalog.cjs");
const root=path.resolve(__dirname,".."), fixture=fs.mkdtempSync(path.join(os.tmpdir(),"xuloom-side-chat-"));
const config=path.join(fixture,"config"),data=path.join(fixture,"data"),work=path.join(fixture,"work");
for(const dir of [config,data,work])fs.mkdirSync(dir);
atomicJSON(path.join(data,"workbench.json"),{projects:[],activeConversation:null,defaultDirectory:work});
const env={...process.env,XULOOM_CONFIG_DIR:config,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,HOME:path.join(fixture,"home"),USERPROFILE:path.join(fixture,"home"),XULOOM_TEST_HEADLESS:"1",NO_PROXY:"127.0.0.1,localhost"};
for(const key of Object.keys(env))if(/^(OPENAI_|ANTHROPIC_)/.test(key)||["ELECTRON_RUN_AS_NODE","XULOOM_DEV_URL","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","PYTHONPATH"].includes(key))delete env[key];
let app,page,mainId,sideId,mainResponse,sideResponse,finishMain,finishSide;
const requests=[],errors=[];
const server=http.createServer((req,res)=>{
 let raw="";req.on("data",chunk=>raw+=chunk);req.on("end",()=>{
  const body=JSON.parse(raw);requests.push(body);
  const question=[...body.messages].reverse().find(m=>m.role==="user")?.content;
  const text=typeof question==="string"?question:JSON.stringify(question);
  res.writeHead(200,{"Content-Type":"text/event-stream"});
  const send=text=>res.write("data: "+JSON.stringify({choices:[{index:0,delta:{content:text},finish_reason:null}]})+"\n\n");
  const finish=()=>{if(!res.destroyed)res.end("data: "+JSON.stringify({choices:[{index:0,delta:{},finish_reason:"stop"}]})+"\n\ndata: [DONE]\n\n");};
  if(text.includes("MAIN_RUNNING")){mainResponse=res;finishMain=()=>{send(" MAIN_FINISHED");finish();};send("MAIN_IS_RUNNING");return;}
  if(text.includes("SIDE_HOLD")){sideResponse=res;finishSide=()=>{send(" SIDE_FINISHED");finish();};send("SIDE_IS_RUNNING");return;}
  send(text.includes("SIDE_QUESTION")?"SIDE_ANSWER":"A selected phrase for a side question. 原文不可翻译。");finish();
 });
});
async function launch(){
 app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
 page=await app.firstWindow();page.setDefaultTimeout(45000);page.on("pageerror",error=>errors.push(error.message));
 await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
 await page.waitForFunction(()=>!document.querySelector(".new-task")?.disabled);
}
async function sendMain(text){
 await page.getByRole("textbox",{name:"任务输入",exact:true}).fill(text);
 await page.getByRole("button",{name:"发送任务",exact:true}).click();
}
async function select(){
 await page.locator('.transcript [data-quote-source]').filter({hasText:"A selected phrase"}).last().evaluate(element=>{
  const node=element.querySelector("p").firstChild,range=document.createRange();range.setStart(node,0);range.setEnd(node,17);
  const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
  element.dispatchEvent(new MouseEvent("mouseup",{bubbles:true,button:0}));
 });
}
(async()=>{try{
 await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
 atomicJSON(path.join(config,"config.json"),{model_provider:"openai",openai_model:"side-fixture",openai_api_key:"OWNED_FIXTURE_KEY",openai_base_url:`http://127.0.0.1:${server.address().port}/v1`,network_mode:"direct",new_session_preferences:{permissionMode:"full",collaborationMode:"solo"}});
 await launch();await page.locator(".new-task").click();await sendMain("BASE_CONTEXT");
 await page.getByText("A selected phrase for a side question. 原文不可翻译。",{exact:true}).last().waitFor();
 await page.waitForFunction(()=>!document.querySelector(".send-button.stop"));
 // Selection adds to existing draft without a model call.
 await page.getByRole("textbox",{name:"任务输入",exact:true}).fill("Keep my question");
 await select();await page.getByRole("button",{name:"添加到对话",exact:true}).click();
 assert((await page.getByRole("textbox",{name:"任务输入",exact:true}).inputValue()).startsWith("Keep my question"));
 assert((await page.getByRole("textbox",{name:"任务输入",exact:true}).inputValue()).includes("> A selected phrase"));
 assert.equal(requests.length,1);
 await sendMain("MAIN_RUNNING");await page.getByText("MAIN_IS_RUNNING",{exact:true}).last().waitFor();
 await select();await page.getByRole("button",{name:"在侧边聊天中提问",exact:true}).click();
 const side=page.getByRole("region",{name:"侧边聊天",exact:true});await side.getByRole("textbox",{name:"侧聊输入",exact:true}).waitFor();
 const initial=await page.evaluate(()=>window.xuloom.invoke("app.initialize"));
 const parent=initial.restored.find(s=>!s.sideChat),child=initial.restored.find(s=>s.sideChat);
 // app.initialize's restoration snapshot is intentionally cached; discover identities from catalog instead.
 const ids=await page.evaluate(()=>window.xuloom.invoke("catalog.refresh"));
 const rows=[...(ids.standalone||[]),...ids.projects.flatMap(p=>p.sessions)];
 const sideRow=rows.find(r=>r.title.startsWith("侧聊："));assert(sideRow);sideId=sideRow.id;
 mainId=rows.find(r=>r.id!==sideId).id;
 assert.equal(requests.length,2,"Opening a side chat must not call a model");
 const sideConfig=await page.evaluate(id=>window.xuloom.invoke("config.get",{session:id}),sideId);assert.equal(sideConfig.model,"side-fixture");
 await side.getByRole("textbox",{name:"侧聊输入",exact:true}).fill("SIDE_QUESTION");
 await side.getByRole("button",{name:"发送侧聊",exact:true}).click();await side.getByText("SIDE_ANSWER",{exact:true}).waitFor();
 assert(mainResponse&&!mainResponse.writableEnded,"The main task must continue");
 await page.waitForFunction(()=>!document.querySelector(".side-chat textarea")?.disabled);
 await side.getByRole("textbox",{name:"侧聊输入",exact:true}).fill("SIDE_HOLD");
 await side.getByRole("button",{name:"发送侧聊",exact:true}).click();await side.getByText("SIDE_IS_RUNNING",{exact:true}).waitFor();
 await side.getByRole("button",{name:"关闭侧边聊天",exact:true}).click();
 assert(sideResponse&&!sideResponse.writableEnded,"Closing the panel must not cancel its task");
 await page.getByRole("button",{name:"打开侧边聊天",exact:true}).click();await side.getByText("SIDE_IS_RUNNING",{exact:true}).waitFor();
 await side.getByRole("button",{name:"停止侧聊",exact:true}).click();await page.waitForFunction(()=>!document.querySelector(".side-chat textarea")?.disabled);
 assert(mainResponse&&!mainResponse.writableEnded,"Stopping the side chat cannot cancel main");
 finishMain();await page.waitForFunction(()=>!document.querySelector(".send-button.stop"));
 await side.getByRole("textbox",{name:"侧聊输入",exact:true}).fill("Unsaved side draft 原文");
 await page.waitForTimeout(800);await app.close();app=null;
 await launch();await page.getByRole("button",{name:"打开侧边聊天",exact:true}).click();
 assert.equal(await page.getByRole("textbox",{name:"侧聊输入",exact:true}).inputValue(),"Unsaved side draft 原文");
 await page.getByRole("region",{name:"侧边聊天",exact:true}).getByText("SIDE_ANSWER",{exact:true}).waitFor();
 // English changes interface labels, preserving the quoted/user text.
 await page.evaluate(()=>localStorage.setItem("xuloom.language","en"));
 await app.close();app=null;await launch();
 await page.getByRole("button",{name:"Open side chat",exact:true}).click();
 assert.equal(await page.getByRole("textbox",{name:"Side chat input",exact:true}).inputValue(),"Unsaved side draft 原文");
 await page.getByRole("region",{name:"Side chat",exact:true}).getByText("SIDE_ANSWER",{exact:true}).waitFor();
 assert.deepEqual(errors,[]);
 fs.mkdirSync(path.join(root,"build"),{recursive:true});
 await page.screenshot({path:path.join(root,"build","side-chat-e2e.png")});
 console.log("Side chat E2E passed: quote, concurrent main, independent cancel, close/reopen, persisted history/draft; no remote providers.");
}finally{
 finishMain?.();finishSide?.();if(app)await app.close();server.closeAllConnections?.();await new Promise(resolve=>server.close(resolve));
 fs.rmSync(fixture,{recursive:true,force:true,maxRetries:8,retryDelay:100});
}})().catch(error=>{console.error(error);process.exitCode=1;});
