const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), http = require('node:http'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-create-project-'));
const data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home'), directory = path.join(fixture, 'existing-folder'), defaultRoot = path.join(fixture, 'default');
for (const dir of [data, config, home, directory]) fs.mkdirSync(dir);
fs.writeFileSync(path.join(directory, 'must-keep.txt'), 'ORIGINAL CONTENT');
fs.writeFileSync(path.join(data, 'workbench.json'), JSON.stringify({ projects: [], defaultDirectory: defaultRoot }));
const errors = [], requests = [];
let app, page;
const server = http.createServer((req, res) => { requests.push(req.url); res.writeHead(500).end('No model request is authorized'); });
const settings = () => JSON.parse(fs.readFileSync(path.join(data, 'workbench.json')));
const invoke = (method, params = {}) => page.evaluate(({ method, params }) => window.xuloom.invoke(method, params), { method, params });
const dialog = () => page.getByRole('dialog', { name: '创建项目', exact: true });
async function open() { await page.locator('.sidebar-action').filter({ hasText: '创建项目' }).click(); await dialog().waitFor(); }
async function capture(name) {
  fs.mkdirSync(path.join(root, 'test-results'), { recursive: true });
  const png = await app.evaluate(async ({ BrowserWindow }) => {
    const web = BrowserWindow.getAllWindows()[0].webContents;
    await web.capturePage(undefined, { stayHidden: true, stayAwake: true });
    await new Promise(resolve => setTimeout(resolve, 100));
    return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64');
  });
  fs.writeFileSync(path.join(root, 'test-results', name + '.png'), Buffer.from(png, 'base64'));
}
async function launch() {
  const env = { ...process.env, XULOOM_TEST_HEADLESS: '1', XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_SKILLS_PATHS: '', HOME: home, USERPROFILE: home };
  for (const key of ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL', 'XULOOM_MODEL_PROVIDER']) delete env[key];
  app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env });
  page = await app.firstWindow(); page.setDefaultTimeout(45000); page.on('pageerror', error => errors.push(error.stack));
  await page.waitForFunction(() => document.querySelector('.new-task')?.disabled === false);
  await app.evaluate(({ dialog }, selected) => {
    globalThis.pickerCalls = 0; globalThis.selectedDirectory = selected; globalThis.cancelPicker = false;
    dialog.showOpenDialog = async (...args) => { const options = args.at(-1); globalThis.pickerCalls++; if (!options.properties.includes('openDirectory')) throw new Error('Expected directory picker'); return { canceled: globalThis.cancelPicker, filePaths: globalThis.cancelPicker ? [] : [globalThis.selectedDirectory] }; };
  }, directory);
}
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.writeFileSync(path.join(config, 'config.json'), JSON.stringify({ model_provider: 'openai', openai_model: '', openai_api_key: '', openai_base_url: `http://127.0.0.1:${server.address().port}/v1` }));
  try {
    await launch();
    const search = page.getByRole('textbox', { name: '搜索项目或会话', exact: true });
    const noMatches = () => page.getByRole('status').filter({ hasText: '未找到匹配的项目或会话。' });
    await page.getByText('新建独立会话，或创建项目开始工作。', { exact: true }).waitFor();
    await search.fill('不存在的会话'); await noMatches().waitFor();
    assert.equal(await page.getByText('新建独立会话，或创建项目开始工作。', { exact: true }).count(), 0);
    await search.fill('   '); await page.getByText('新建独立会话，或创建项目开始工作。', { exact: true }).waitFor();
    await search.fill(''); await open();
    assert.equal(await app.evaluate(() => globalThis.pickerCalls), 0, 'Opening the app dialog must not invoke the OS picker');
    assert.equal(await dialog().getByRole('button', { name: '创建项目', exact: true }).isDisabled(), true);
    assert(await page.getByLabel('项目名称', { exact: true }).evaluate(element => element === document.activeElement));
    await capture('create-project-empty');
    await page.keyboard.press('Escape'); await dialog().waitFor({ state: 'hidden' });
    assert.deepEqual(settings().projects, []); assert(!fs.existsSync(defaultRoot));
    await open(); await page.getByLabel('项目名称', { exact: true }).fill('科研资料');
    await app.evaluate(() => { globalThis.cancelPicker = true; });
    await dialog().getByRole('button', { name: '添加文件夹', exact: true }).click();
    assert.equal(await page.getByLabel('项目名称', { exact: true }).inputValue(), '科研资料');
    assert.deepEqual(settings().projects, []); assert.equal(await dialog().locator('.project-source-selected').count(), 0);
    await app.evaluate(() => { globalThis.cancelPicker = false; });
    await dialog().getByRole('button', { name: '添加文件夹', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.project-source-selected') || document.querySelector('.create-project-dialog [role="alert"]'));
    if (await dialog().getByRole('alert').count()) throw new Error(await dialog().getByRole('alert').innerText());
    await dialog().locator('.project-source-selected').waitFor();
    assert.equal(await app.evaluate(() => globalThis.pickerCalls), 2);
    assert.equal(await page.getByLabel('项目名称', { exact: true }).inputValue(), '科研资料');
    assert.deepEqual(settings().projects, [], 'Selecting a folder alone must not create a project');
    await capture('create-project-selected');
    await dialog().getByRole('button', { name: '创建项目', exact: true }).click(); await dialog().waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '打开项目 科研资料', exact: true }).waitFor();
    assert.equal(settings().activeConversation.project, fs.realpathSync(directory));
    assert.equal(fs.readFileSync(path.join(directory, 'must-keep.txt'), 'utf8'), 'ORIGINAL CONTENT');
    await page.getByRole('textbox', { name: '任务输入' }).fill('保留这个项目的草稿');
    await open(); await page.getByLabel('项目名称', { exact: true }).fill('写作计划');
    await dialog().getByRole('button', { name: '创建项目', exact: true }).click(); await dialog().waitFor({ state: 'hidden' });
    const managed = settings().activeConversation.project;
    assert.equal(path.dirname(managed), fs.realpathSync(defaultRoot)); assert(fs.statSync(managed).isDirectory());
    assert.notEqual(managed, directory); assert.equal(await app.evaluate(() => globalThis.pickerCalls), 2);
    await search.fill('科研资料'); await page.getByRole('button', { name: '打开项目 科研资料', exact: true }).waitFor();
    assert.equal(await noMatches().count(), 0);
    await search.fill('不存在的会话'); await noMatches().waitFor();
    assert.equal(await page.locator('.project-group, .standalone-group').count(), 0);
    await capture('sidebar-search-no-match');
    await search.fill(''); await page.getByRole('button', { name: '打开项目 写作计划', exact: true }).waitFor();
    assert.equal(await noMatches().count(), 0);
    const before = fs.readdirSync(defaultRoot);
    await assert.rejects(invoke('project.create', { name: '   ' }), /项目名称/);
    assert.deepEqual(fs.readdirSync(defaultRoot), before);
    await page.getByRole('button', { name: '打开项目 科研资料', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('.new-task').disabled);
    assert.equal(await page.getByRole('textbox', { name: '任务输入' }).inputValue(), '保留这个项目的草稿');
    assert.equal(await dialog().count(), 0); assert.equal(await app.evaluate(() => globalThis.pickerCalls), 2);
    await open(); await page.getByLabel('项目名称', { exact: true }).fill('无效源文件夹');
    await app.evaluate(() => { globalThis.selectedDirectory += '/must-keep.txt'; });
    await dialog().getByRole('button', { name: '添加文件夹', exact: true }).click();
    await dialog().getByRole('button', { name: '创建项目', exact: true }).click(); await dialog().getByRole('alert').waitFor();
    assert.equal(settings().projects.length, 2, 'Invalid source must not register a project');
    await dialog().getByRole('button', { name: '取消', exact: true }).click();
    await app.close(); app = null; await launch();
    await page.getByRole('button', { name: '打开项目 科研资料', exact: true }).waitFor();
    await page.getByRole('button', { name: '打开项目 写作计划', exact: true }).waitFor();
    assert.deepEqual(requests, []); assert.deepEqual(errors, []);
    console.log('PASS: search distinguishes empty catalog from unmatched results, matches projects, and restores results on clear; app project dialog, cancel, validation, files/drafts and restart verified; no model traffic');
  } finally { if (app) await app.close().catch(() => {}); server.closeAllConnections(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
