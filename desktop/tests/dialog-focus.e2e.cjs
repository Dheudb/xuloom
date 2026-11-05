const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const { _electron: electron } = require('playwright');
const { SessionCatalog, atomicJSON } = require('../electron/catalog.cjs');
const root = path.resolve(__dirname, '..');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-dialog-focus-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const directory of [data, config, home]) fs.mkdirSync(directory);
atomicJSON(path.join(config, 'config.json'), { model_provider:'openai', openai_model:'fixture', openai_api_key:'fixture', openai_base_url:'http://127.0.0.1:9/v1' });
let settings = { projects:[] };
const catalog = new SessionCatalog(() => settings, value => { settings = value; }, data);
const row = catalog.createStandalone(fixture);
atomicJSON(catalog.file(row.project,row.sessionId), { messages:[], metadata:{} });
settings.activeConversation = { project:row.project, resumeId:row.sessionId, scope:'standalone' };
atomicJSON(path.join(data,'workbench.json'),settings);
const env = { ...process.env, XULOOM_DATA_DIR:data, XULOOM_USER_DATA:data, XULOOM_CONFIG_DIR:config, HOME:home, USERPROFILE:home, XULOOM_TEST_HEADLESS:'1', XULOOM_SKILLS_PATHS:'', PYTHONPATH:path.dirname(root) };
for (const key of ['ELECTRON_RUN_AS_NODE','XULOOM_DEV_URL','OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_MODEL','XULOOM_MODEL_PROVIDER','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL']) delete env[key];
let app;
(async () => {
  try {
    app = await electron.launch({ executablePath:process.env.XULOOM_TEST_EXECUTABLE, args:process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
    const page = await app.firstWindow(); page.setDefaultTimeout(20000);
    const errors = []; page.on('pageerror',error=>errors.push(error.message));
    const trigger = page.getByRole('button',{name:'添加工具与模式',exact:true});
    await trigger.waitFor();
    for (const item of ['上下文','执行隔离']) {
      await trigger.click(); await page.getByRole('menuitem',{name:new RegExp('^'+item)}).click();
      await page.getByRole('dialog').waitFor();
      for (const key of ['Tab','Shift+Tab',...Array(12).fill('Tab')]) {
        await page.keyboard.press(key);
        assert(await page.evaluate(()=>!!document.activeElement.closest('[role="dialog"]')), 'Keyboard focus escaped '+item);
      }
      await page.keyboard.press('Escape');
      await page.getByRole('dialog').waitFor({state:'detached'});
      assert(await trigger.evaluate(element=>element===document.activeElement),'Focus was not returned');
    }
    await page.getByRole('button',{name:'设置与模型',exact:true}).click();
    await page.getByRole('tab',{name:'任务准备',exact:true}).click();
    const readiness=page.getByRole('region',{name:'任务准备检查'});
    await page.getByText('2. 文件与产物',{exact:true}).waitFor();
    assert(await page.getByText('3. 工具与验收',{exact:true}).isVisible());
    assert.equal(await page.getByRole('alert').count(),0,'Preflight must work with the real local bridge');
    await page.getByRole('button',{name:'重新检查',exact:true}).click();
    await page.getByRole('button',{name:'重新检查',exact:true}).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({state:'detached'});
    await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].webContents.setZoomFactor(2));
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal page overflow at 200% zoom');
    assert.deepEqual(errors,[]);
    console.log('Dialog focus E2E passed: entry, forward/backward loop, Escape, focus return, 200% zoom; no model requests');
  } finally { if (app) await app.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
