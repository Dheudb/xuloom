const { _electron: electron } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-extension-entry-'));
const home = path.join(fixture, 'home');
const config = path.join(fixture, 'config');
const data = path.join(fixture, 'data');
for (const directory of [home, config, data]) fs.mkdirSync(directory);
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
  !/^(OPENAI_|ANTHROPIC_|XULOOM_DEV_URL$|XULOOM_MODEL_PROVIDER$|ELECTRON_RUN_AS_NODE$)/i.test(name)));
Object.assign(env, { HOME: home, USERPROFILE: home, XULOOM_DATA_DIR: data,
  XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: '1' });

async function run() {
  const executablePath = process.env.XULOOM_TEST_EXECUTABLE;
  const app = await electron.launch({ executablePath, args: executablePath ? [] : [root], env });
  const failures = [];
  try {
    const page = await app.firstWindow();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(20000);
    await page.locator('.new-task').click();
    await page.locator('.session-item.active').waitFor();
    page.setDefaultTimeout(5000);
    const input = page.getByRole('textbox', { name: '任务输入', exact: true });
    const settings = page.getByRole('dialog', { name: '工作台设置' });
    const skills = settings.getByRole('tab', { name: 'Skills', exact: true });
    const openExtensions = () => page.getByRole('button', { name: '扩展', exact: true }).click();
    const openPicker = async () => {
      await page.getByRole('button', { name: '添加工具与模式', exact: true }).click();
      await page.getByRole('menuitem', { name: /^Skills/ }).click();
    };
    async function check(name, verify) {
      try {
        await verify();
      } catch (error) {
        failures.push(new Error(name, { cause: error }));
      } finally {
        if (await settings.isVisible()) await settings.getByRole('button', { name: '关闭设置', exact: true }).click();
      }
    }

    await check('Opening Extensions focuses its selected tab', async () => {
      await openExtensions();
      await skills.waitFor();
      await skills.evaluate(element => { if (element !== document.activeElement) throw new Error('Skills tab lacks focus'); });
      await page.keyboard.press('ArrowDown');
      await settings.getByRole('tab', { name: 'Subagents', exact: true, selected: true }).waitFor();
    });
    await check('Assigning a subagent preserves the draft', async () => {
      const draft = '已有草稿\n继续核对测试记录。';
      await input.fill(draft);
      await openExtensions();
      await settings.getByRole('tab', { name: 'Subagents', exact: true }).click();
      const row = settings.locator('.extension-row').filter({ has: page.getByText('general-purpose', { exact: true }) });
      await row.getByRole('button', { name: /分配任务/ }).click();
      await settings.waitFor({ state: 'hidden' });
      assert.equal(await input.inputValue(), `请使用 general-purpose 子代理完成以下任务：\n${draft}`);
    });
    await check('The Skills shortcut opens from a caret inside the draft', async () => {
      const draft = 'keep this draft';
      await input.fill(draft);
      await input.press('Home');
      await input.press('ArrowRight');
      await openPicker();
      assert.equal(await input.inputValue(), `${draft} $`);
      await page.getByRole('listbox', { name: '可用技能' }).waitFor();
      assert.equal(await input.evaluate(element => element.selectionStart), draft.length + 2);
      await input.press('Escape');
    });
    await check('The empty skill picker points to Extensions', async () => {
      await input.fill('$nomatchfixturexyz');
      await page.getByText('没有匹配的 Skill。可在「扩展 → Skills → 导入目录」导入，或放入 .xuloom/skills/<名称>/SKILL.md。', { exact: true }).waitFor();
    });

    await app.evaluate(({ ipcMain }) => {
      const original = ipcMain._invokeHandlers.get('workbench:invoke');
      ipcMain.removeHandler('workbench:invoke');
      ipcMain.handle('workbench:invoke', (event, method, params) => {
        if (method === 'config.get' && params?.session) throw new Error('FIXTURE_MODEL_CONFIG_UNAVAILABLE');
        return original(event, method, params);
      });
    });
    await check('A failed model configuration read leaves Extensions usable', async () => {
      await openExtensions();
      await settings.getByRole('button', { name: '导入目录', exact: true }).waitFor();
      assert.equal(await settings.getByText(/FIXTURE_MODEL_CONFIG_UNAVAILABLE/).isVisible(), false);
      await settings.getByRole('tab', { name: '模型连接', exact: true }).click();
      await settings.getByRole('alert').filter({ hasText: 'FIXTURE_MODEL_CONFIG_UNAVAILABLE' }).waitFor();
      assert.equal(await settings.getByRole('button', { name: '保存配置', exact: true }).count(), 0);
      await settings.getByRole('tab', { name: '工作区', exact: true }).click();
      const directory = settings.getByRole('textbox', { name: '默认工作目录' });
      await page.waitForFunction(element => !element.disabled && element.value, await directory.elementHandle());
    });
    assert.deepEqual(errors, []);
    if (failures.length) throw new AggregateError(failures, 'Extension entry regressions');
    console.log('PASS: drafts, skill picker caret and import hint, settings focus and independent settings loading.');
  } finally {
    await app.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
