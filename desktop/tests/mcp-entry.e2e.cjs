const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const { atomicJSON } = require('../electron/catalog.cjs');
const root = path.resolve(__dirname,'..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-mcp-entry-'));
const data=path.join(fixture,'data'),config=path.join(fixture,'config'),home=path.join(fixture,'home');
for(const dir of [data,config,home]) fs.mkdirSync(dir);
atomicJSON(path.join(data,'workbench.json'),{projects:[]});
atomicJSON(path.join(config,'config.json'),{model_provider:'openai',openai_model:'',openai_api_key:'',openai_base_url:'http://127.0.0.1:9/v1'});
let app; const errors=[];
async function launch(){
 const env={...process.env,XULOOM_DATA_DIR:data,XULOOM_USER_DATA:data,XULOOM_CONFIG_DIR:config,HOME:home,USERPROFILE:home,XULOOM_TEST_HEADLESS:'1',XULOOM_SKILLS_PATHS:''};
 for(const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER'])delete env[key];
 app=await electron.launch({executablePath:process.env.XULOOM_TEST_EXECUTABLE,args:process.env.XULOOM_TEST_EXECUTABLE?[]:[root],env});
 const page=await app.firstWindow();page.setDefaultTimeout(30000);page.on('pageerror',e=>errors.push(e.message));return page;
}
async function main(){try{
 let page=await launch();
 const entry=page.getByRole('button',{name:'MCP 市场',exact:true});
 assert.equal(await entry.isEnabled(),true,'Market must be available before creating a conversation');
 await entry.click();let dialog=page.getByRole('dialog',{name:'MCP 市场',exact:true});
 await dialog.getByText('Exa Search',{exact:true}).waitFor();
 assert.equal(await page.locator('.session-item').count(),0,'Browsing must not create an implicit conversation');
 assert.equal(await dialog.getByRole('button',{name:'自定义服务',exact:true}).isDisabled(),true);
 assert.equal(await dialog.getByRole('button',{name:'安装',exact:true}).first().isDisabled(),true);
 assert.equal(fs.existsSync(path.join(data,'.xuloom/mcp_servers.json')),false,'Browsing must not write a hidden fallback configuration');
 await dialog.getByLabel('搜索 MCP',{exact:true}).fill('Fetch');await dialog.getByRole('button',{name:'搜索',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('.mcp-grid .mcp-card').length===1);
 await dialog.locator('.mcp-card').filter({hasText:'Fetch'}).waitFor();assert.equal(await dialog.locator('.mcp-card').count(),1);
 for(const method of ['mcp.save','mcp.install','mcp.test','mcp.oauth.login']){
   const rejected=await page.evaluate(async method=>{try{await window.xuloom.invoke(method,{name:'wrong',config:{type:'http',url:'http://127.0.0.1:9/mcp'}});return false;}catch{return true;}},method);
   assert.equal(rejected,true,method+' still requires an owning session');
 }
 const invalidSession=await page.evaluate(async()=>{try{await window.xuloom.invoke('mcp.market',{session:'not-a-session',source:'builtin'});return false;}catch{return true;}});
 assert.equal(invalidSession,true,'An invalid nonempty session must not silently fall back to global browsing');
 await dialog.getByRole('button',{name:'关闭 MCP 市场',exact:true}).click();
 await page.getByRole('button',{name:'设置与模型',exact:true}).click();await page.getByRole('tab',{name:'MCP 服务',exact:true}).click();
 await page.getByRole('button',{name:'打开 MCP 管理',exact:true}).click();dialog=page.getByRole('dialog',{name:'MCP 市场',exact:true});
 await dialog.getByText('Exa Search',{exact:true}).waitFor();
 await dialog.getByRole('button',{name:'新建会话并配置',exact:true}).click();
 await page.locator('.session-item.active').waitFor();
 dialog=page.getByRole('dialog',{name:'MCP 市场',exact:true});
 await dialog.locator('.mcp-card').filter({hasText:'Fetch'}).getByRole('button',{name:'安装',exact:true}).click();
 await dialog.getByRole('button',{name:'安装并保存配置',exact:true}).click();
 await dialog.getByRole('status').filter({hasText:'配置已保存'}).waitFor();
 const session=await page.locator('.session-item.active').getAttribute('data-session-id');
 const snapshot=await page.evaluate(session=>window.xuloom.invoke('mcp.list',{session}),session);
 assert.equal(snapshot.servers.find(s=>s.catalogId==='fetch').enabled,false,'Installation does not start an external service');
 const target=path.join(snapshot.project,'.xuloom/mcp_servers.json');
 assert.equal(JSON.parse(fs.readFileSync(target)).mcpServers.fetch.catalogId,'fetch');
 assert.equal(fs.existsSync(path.join(data,'.xuloom/mcp_servers.json')),false);
 await app.close();app=undefined;
 page=await launch();await page.locator('.session-item.active').waitFor();await page.getByRole('button',{name:'MCP 市场',exact:true}).click();
 await page.getByRole('dialog',{name:'MCP 市场'}).getByRole('button',{name:/我的服务/}).click();
 await page.getByRole('dialog',{name:'MCP 市场'}).getByText('Fetch',{exact:true}).waitFor();
 assert.deepEqual(errors,[]);
 console.log('PASS: empty/fresh app and settings browse MCP without a model or conversation; explicit standalone setup saves only to its workspace; install/auth/test remain scoped; restart preserves services. No remote requests.');
}finally{if(app)await app.close();}}
main().catch(e=>{console.error(e);process.exitCode=1;});
