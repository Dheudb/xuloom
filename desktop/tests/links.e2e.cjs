const { historyRoot } = require("../electron/history-storage.cjs");
const { _electron: electron } = require("playwright");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), assert = require("node:assert/strict");
const { atomicJSON } = require("../electron/catalog.cjs");
const root = path.resolve(__dirname, "..");
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "xuloom-links-e2e-"));
const project = path.join(fixture, "project"), data = path.join(fixture, "profile"), config = path.join(fixture, "config");
for (const directory of [project, data, config]) fs.mkdirSync(directory);
const history = path.join(project, ".xuloom/sessions/links.json");
fs.mkdirSync(path.join(project, 'docs'));
fs.writeFileSync(path.join(project, 'docs/README'), 'one\ntwo\nthree\nfour\nfive\nsix\n');
fs.writeFileSync(path.join(project, 'docs/index.md'), '[Relative nested](README:2)');
fs.writeFileSync(path.join(project, 'Dockerfile'), 'FROM scratch');
fs.writeFileSync(path.join(fixture, 'outside.txt'), 'OUTSIDE SECRET');
atomicJSON(history, {"messages": [{"role": "user", "content": "查看来源链接", "_uuid": "question"}, {"role": "assistant", "content": "Sources:\n- [Chemistry paper](https://arxiv.org/abs/2506.17238)\n- [HTTP source](http://example.org/paper?from=search#abstract)\n- https://example.org/bare\n- [Unsafe](javascript:alert(1))\n- ![Image](zzc)\n- [File line](docs/README:3:2)\n- [Folder](docs/)\n- `Dockerfile`\n- [Nested document](docs/index.md)\n- [`README`](https://github.com/Dheudb/xuloom)\n- [Outside](../outside.txt)"}], "metadata": {"turn_traces": [{"id": "link-trace", "userMessageId": "question", "status": "completed", "elapsedMs": 1000, "steps": [{"kind": "text", "text": "[Process file](docs/README#L2)"}, {"kind": "tool", "tool": {"id": "read", "name": "file_read_tool", "status": "completed", "input": {"file_path": "docs/README"}, "result": "[Tool file](docs/README#L6)"}}], "answer": "Sources:\n- [Chemistry paper](https://arxiv.org/abs/2506.17238)\n- [HTTP source](http://example.org/paper?from=search#abstract)\n- https://example.org/bare\n- [Unsafe](javascript:alert(1))\n- ![Image](zzc)\n- [File line](docs/README:3:2)\n- [Folder](docs/)\n- `Dockerfile`\n- [Nested document](docs/index.md)\n- [`README`](https://github.com/Dheudb/xuloom)\n- [Outside](../outside.txt)", "files": []}], "subagents": [{"agent_id": "general-1", "description": "Link worker", "status": "completed", "result": "[Worker file](docs/README#L4)"}]}});
atomicJSON(path.join(historyRoot(project, data), 'sessions/subagents/general-1.json'), {
  agent_id:'general-1', session_id:'links', messages:[{role:'assistant',content:'[History file](docs/README:5)'}],
  metadata:{delegation:{owner_session_id:'links'}}
});
const original = fs.readFileSync(history);
atomicJSON(path.join(data, "workbench.json"), { projects: [project], workspace: [{ project, resumeId: "links", active: true }] });
let app, page;
async function opened(count) {
  for (let i = 0; i < 50; i++) {
    const urls = await app.evaluate(() => globalThis.linkOpens);
    if (urls.length === count) return urls;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.fail("External link did not reach the browser opener");
}
async function main() {
  try {
    const env = { ...process.env, USERPROFILE: path.join(fixture, "home"), HOME: path.join(fixture, "home"), XULOOM_USER_DATA: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: "1" };
    for (const key of ["ELECTRON_RUN_AS_NODE", "XULOOM_DEV_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "XULOOM_MODEL_PROVIDER"]) delete env[key];
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 30000 });
    page = await app.firstWindow(); page.setDefaultTimeout(30000);
    const errors = []; page.on("pageerror", (error) => errors.push(error.message));
    await page.locator(".session-item.active").waitFor();
    await app.evaluate(({ shell }) => {
      globalThis.linkOpens = [];
      shell.openExternal = async (url) => {
        if (globalThis.failLinkOpen) throw new Error("fixture browser unavailable");
        globalThis.linkOpens.push(url);
      };
    });
    const initialURL = page.url();
    async function expectLine(label, line) {
      console.log('Opening reference:', label, line);
      await page.getByRole('button', { name:label, exact:true }).click();
      await page.locator(`.source-line.source-selected[data-source-line="${line}"]`).waitFor();
      if (label === 'File line') {
        const png=await app.evaluate(async ({BrowserWindow}) => {
          const web=BrowserWindow.getAllWindows()[0].webContents;
          await web.capturePage(undefined,{stayHidden:true,stayAwake:true}); await new Promise(resolve=>setTimeout(resolve,100));
          return (await web.capturePage(undefined,{stayHidden:true,stayAwake:true})).toPNG().toString('base64');
        });
        fs.mkdirSync(path.join(root,'test-results'),{recursive:true});
        fs.writeFileSync(path.join(root,'test-results/references-ui.png'),Buffer.from(png,'base64'));
      }
      await page.getByRole('button', { name:'关闭文件预览', exact:true }).click();
    }
    assert.equal(await page.getByRole('link', {name:'README',exact:true}).locator('svg').count(),1);
    assert.equal(await page.getByRole('link', {name:'README',exact:true}).getByRole('button').count(),0, 'Code in link labels must not create nested navigation');
    await expectLine('File line',3);
    await page.getByRole('button',{name:'Folder',exact:true}).click();
    await page.getByRole('region',{name:'文件夹预览'}).getByRole('button',{name:'查看文件 README',exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭文件预览',exact:true}).click();
    await page.getByRole('button',{name:'Dockerfile',exact:true}).click();
    await page.locator('.file-source').getByText('FROM scratch',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭文件预览',exact:true}).click();
    await page.getByRole('button',{name:'Nested document',exact:true}).click();
    await expectLine('Relative nested',2);
    await page.getByRole('button',{name:'Outside',exact:true}).click();
    await page.getByRole('alert').filter({hasText:/当前|预览范围/}).first().waitFor();
    assert.equal(await page.getByText('OUTSIDE SECRET',{exact:true}).count(),0);
    await page.getByLabel('查看本轮执行过程').click();
    await expectLine('Process file',2);
    await page.locator('.turn-tool > summary').click(); await expectLine('Tool file',6);
    if (await page.getByRole('button',{name:'展开能力面板',exact:true}).count()) await page.getByRole('button',{name:'展开能力面板',exact:true}).click();
    await page.getByRole('button',{name:'代理',exact:true}).click();
    await page.getByText('查看结果',{exact:true}).click(); await expectLine('Worker file',4);
    await page.getByRole('button',{name:'查看完整对话历史',exact:true}).click();
    await expectLine('History file',5);
    assert.equal(await page.getByRole('dialog',{name:'子代理完整历史'}).count(),0);

    await page.getByRole("link", { name: "Chemistry paper", exact: true }).click();
    assert.deepEqual(await opened(1), ["https://arxiv.org/abs/2506.17238"]);
    await page.getByRole("link", { name: "HTTP source", exact: true }).focus();
    await page.keyboard.press("Enter");
    assert.equal((await opened(2))[1], "http://example.org/paper?from=search#abstract");
    await page.getByRole("link", { name: "https://example.org/bare", exact: true }).click({ button: "middle" });
    assert.equal((await opened(3))[2], "https://example.org/bare");
    assert.equal(await page.getByRole("link", { name: "Unsafe", exact: true }).count(), 0);
    assert.equal(await page.locator(".markdown img").count(), 0);
    for (const url of ["javascript:alert(1)", "file:///C:/Windows/system.ini", "data:text/html,test", "https://user:password@example.org/"]) {
      assert.equal(await page.evaluate(async (url) => {
        try { await window.xuloom.invoke("external.open", { url }); return true; } catch { return false; }
      }, url), false);
    }
    await app.evaluate(() => { globalThis.failLinkOpen = true; });
    await page.getByRole("link", { name: "Chemistry paper", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "无法打开链接" }).waitFor();
    assert.equal(page.url(), initialURL);
    assert.equal(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
    assert.deepEqual(fs.readFileSync(path.join(historyRoot(project, data), "sessions/links.json")), original, "Existing transcripts must remain unchanged");
    assert.deepEqual(errors, []);
    console.log("Link E2E passed: saved Sources links, HTTP/HTTPS, bare URLs, mouse/keyboard/middle click, opener failures, protocol validation, icons, nested labels, files/directories, line positions, document-relative links, process and child history references, workspace boundaries and no app navigation");
  } finally { if (app) await app.close().catch(() => {}); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
