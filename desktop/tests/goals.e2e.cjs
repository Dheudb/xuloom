const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), http = require("node:http"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs"), { historyRoot } = require("../electron/history-storage.cjs");
const root = path.resolve(__dirname, ".."), fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-goals-e2e-"));
const data = path.join(fixture, "data"), project = path.join(fixture, "project"), config = path.join(fixture, "config"), home = path.join(fixture, "home");
for (const directory of [data, project, config, home]) fs.mkdirSync(directory);
const env = { ...process.env, XULOOM_DATA_DIR: data, XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config,
  USERPROFILE: home, HOME: home, XULOOM_TEST_HEADLESS: "1", XULOOM_SKILLS_PATHS: "", PYTHONPATH: path.dirname(root) };
for (const name of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[name];
const checkpoint = path.join(historyRoot(project, data), "sessions/goals.json");
atomicJSON(checkpoint, { messages: [], metadata: {} });
atomicJSON(path.join(data, "workbench.json"), { projects: [project], activeConversation: { project, resumeId: "goals", scope: "project" } });
let app, page, requests = 0, delayed = 0; const errors = [];
const server = http.createServer((req, res) => {
  let raw = ""; req.on("data", c => raw += c); req.on("end", () => {
    const body = JSON.parse(raw);
    if (!body.stream) return res.writeHead(200, {"Content-Type":"application/json"}).end(JSON.stringify({choices:[{message:{role:"assistant",content:"memory"}}],usage:{prompt_tokens:10,completion_tokens:1,total_tokens:11}}));
    requests++;
    const last = body.messages.at(-1), goal = JSON.stringify(last.content).includes("Continue the explicitly authorized goal");
    const wake = JSON.stringify(last.content).includes("WAKE_MESSAGE_PROOF");
    if (wake) delayed++;
    res.writeHead(200, {"Content-Type":"text/event-stream"});
    const delta = goal ? {tool_calls:[{index:0,id:"goal-report-"+requests,type:"function",function:{name:"goal_report_tool",arguments:JSON.stringify({status:"complete",summary:"Evidence provided for user review",evidence:["Goal response"]})}}]} : {content:wake ? "WAKE_RESULT_PROOF" : "GOAL_RESULT_PROOF"};
    res.end("data: "+JSON.stringify({choices:[{index:0,delta,finish_reason:null}]})+"\n\ndata: "+JSON.stringify({choices:[{index:0,delta:{},finish_reason:goal?"tool_calls":"stop"}]})+'\n\ndata: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\ndata: [DONE]\n\n');
  });
});
async function launch() {
  app = await electron.launch({ executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env,timeout:30000 });
  page = await app.firstWindow(); page.setDefaultTimeout(30000); page.on("pageerror", e=>errors.push(e.message));
  await page.locator(".session-item.active").waitFor();

}
async function main() {
  try {
    await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
    atomicJSON(path.join(config,"config.json"),{model_provider:"openai",openai_model:"fixture",openai_api_key:"fixture",openai_base_url:`http://127.0.0.1:${server.address().port}/v1`});
    await launch();
    await page.getByRole('textbox',{name:'任务输入'}).fill('GOAL_OBJECTIVE_PROOF');
    await page.getByRole('button',{name:'添加工具与模式',exact:true}).click();
    await page.getByRole('menuitem',{name:/^目标 /}).click();
    const limited = process.env.XULOOM_TEST_GOAL_LIMITS === '1';
    if (limited) {
      await page.locator('.goal-limits summary').click();
      await page.getByLabel('累计输出 tokens',{exact:true}).fill('12000');
      await page.getByLabel('最多执行轮数',{exact:true}).fill('3');
    }
    await page.getByRole('button',{name:'发送任务',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.send-button.stop'));
    for(let i=0;i<100 && JSON.parse(fs.readFileSync(checkpoint)).metadata.goal?.status!=='completed';i++) await page.waitForTimeout(100);
    assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.status,'completed');
    assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.budget.output_tokens,limited ? 12000 : null);
    if (limited) assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.budget.rounds,3);
    assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.accepted_by,'model_report');
    const completed=page.getByRole('button',{name:'查看目标完成依据',exact:true});
    await completed.waitFor(); assert((await completed.innerText()).includes('模型报告完成'));
    await completed.click();
    await page.getByRole('region',{name:'目标完成依据',exact:true}).getByText('这是模型对任务完成情况的报告，未经过独立程序检查或用户验收。',{exact:true}).waitFor();
    fs.mkdirSync(path.join(root,'test-results'),{recursive:true});
    const proof=await app.evaluate(async({BrowserWindow})=>{const web=BrowserWindow.getAllWindows()[0].webContents;await web.capturePage(undefined,{stayHidden:true,stayAwake:true});return(await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');});
    fs.writeFileSync(path.join(root,'test-results/goals-completion-proof.png'),Buffer.from(proof,'base64'));
    await page.getByRole('button',{name:'收起已完成目标',exact:true}).click();
    assert.equal(await page.getByRole('region',{name:'持续目标',exact:true}).count(),0);
    assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.status,'completed');
    await page.getByRole('button',{name:'添加工具与模式',exact:true}).click();
    await page.getByRole('menuitem',{name:/定时唤醒/}).click();
    await page.getByRole("textbox",{name:"定时消息",exact:true}).fill("WAKE_MESSAGE_PROOF");
    await page.getByLabel("多少分钟后").fill("0");
    await page.getByRole("button",{name:"保存唤醒"}).click();
    await page.getByText("WAKE_RESULT_PROOF",{exact:true}).waitFor();
    await page.waitForFunction(()=>!document.querySelector(".send-button.stop"));
    await page.waitForTimeout(5500); assert.equal(delayed,1);
    const count=requests; await app.close(); app=undefined; await launch();
    assert.equal(requests,count); assert.equal(delayed,1);
    assert.equal(JSON.parse(fs.readFileSync(checkpoint)).metadata.goal.status,"completed");
    await page.getByRole('button',{name:'查看目标完成依据',exact:true}).waitFor();
    const jobs=JSON.parse(fs.readFileSync(path.join(data,"schedules.json"))).items;
    assert.equal(Object.values(jobs)[0].status,"completed");
    assert.deepEqual(errors,[]);
    console.log("Goals E2E passed: real desktop creation, explicit acceptance, durable budgets/state, timed message to original conversation, no duplicate delivery and restart without model replay");
  } catch(error) { if(page) console.error((await page.locator("body").innerText()).slice(-9000)); throw error; }
  finally { if(app) await app.close(); server.closeAllConnections(); server.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
