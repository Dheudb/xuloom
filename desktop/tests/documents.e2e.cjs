const { _electron: electron } = require('playwright');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { atomicJSON } = require('../electron/catalog.cjs');
const { resolveEnginePython } = require('../electron/runtime.cjs');
const root = path.resolve(__dirname, '..'), repository = path.dirname(root);
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'xuloom-documents-e2e-'));
const project = path.join(fixture, 'project'), data = path.join(fixture, 'data'), config = path.join(fixture, 'config'), home = path.join(fixture, 'home');
for (const folder of [project, data, config, home]) fs.mkdirSync(folder);
const python = resolveEnginePython({ packaged: false, projectRoot: repository });
execFileSync(python, ['-X', 'utf8', '-c', 'import sys;sys.path.insert(0,"tests");from preview_fixtures import create_fixtures;create_fixtures(sys.argv[1])', project], { cwd: repository, windowsHide: true });
const files = ['paper.pdf', 'report.docx', 'sheets.xlsx', 'slides.pptx', 'data.csv', 'data.tsv', 'data.json', 'data.jsonl', 'data.yaml', 'data.xml', 'analysis.ipynb', 'open.odt', 'open.ods', 'open.odp', 'notes.rtf', 'vector.svg', 'scan.tiff', 'icon.ico', 'files.zip'];
atomicJSON(path.join(project, '.xuloom/sessions/documents.json'), { messages: [{ role: 'user', content: 'Inspect the generated files' }, { role: 'assistant', content: files.map(name => `[${name}](${name})`).join('\n\n') }], metadata: {} });
atomicJSON(path.join(data, 'workbench.json'), { projects: [project], workspace: [{ project, resumeId: 'documents', active: true }], layout: { inspectorWidth: 680, reviewTreeWidth: 160 } });
let app;
async function main() {
  try {
    const env = { ...process.env, USERPROFILE: home, HOME: home, XULOOM_USER_DATA: data, XULOOM_DATA_DIR: data, XULOOM_CONFIG_DIR: config, XULOOM_TEST_HEADLESS: '1' };
    for (const key of Object.keys(env)) if (/^(OPENAI_|ANTHROPIC_)/.test(key) || ['ELECTRON_RUN_AS_NODE', 'XULOOM_DEV_URL', 'XULOOM_MODEL_PROVIDER'].includes(key)) delete env[key];
    app = await electron.launch({ executablePath: process.env.XULOOM_TEST_EXECUTABLE, args: process.env.XULOOM_TEST_EXECUTABLE ? [] : [root], env, timeout: 45000 });
    const page = await app.firstWindow(); page.setDefaultTimeout(20000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const region = page.getByRole('region', { name: '文件预览', exact: true });
    async function open(name) {
      if (await region.count()) await region.getByRole('button', { name: '关闭文件预览', exact: true }).click();
      await page.locator('.message.assistant .inline-file-link').filter({ hasText: name }).first().click();
      try { await region.locator('.document-content').waitFor(); } catch (error) { console.error('Preview failed', name, await region.innerText()); throw error; }
      assert.equal(await region.getByRole('tab', { name: '预览', exact: true }).getAttribute('aria-selected'), 'true');
    }
    async function image() { await page.waitForFunction(() => { const img = document.querySelector('.document-content img'); return img?.complete && img.naturalWidth > 0; }); }
    async function shot(name) {
      const png = await app.evaluate(async ({ BrowserWindow }) => { const web = BrowserWindow.getAllWindows()[0].webContents; await web.capturePage(undefined, { stayHidden: true, stayAwake: true }); return (await web.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'); });
      fs.mkdirSync(path.join(root, 'test-results'), { recursive: true }); fs.writeFileSync(path.join(root, 'test-results', name), Buffer.from(png, 'base64'));
    }
    await open('paper.pdf'); await image(); await shot('documents-pdf.png');
    await region.getByRole('button', { name: '下一页', exact: true }).click(); await region.getByAltText('文档第 2 页', { exact: true }).waitFor(); await image();
    await region.getByLabel('预览缩放', { exact: true }).selectOption('150'); assert.equal(await region.locator('.document-page-image').evaluate(img => img.style.width), '150%');
    const id = await page.locator('.session-item.active').getAttribute('data-session-id');
    const invalid = await page.evaluate(async id => { try { await window.xuloom.invoke('files.documentInfo', { session: id, path: '../outside.pdf' }); return false; } catch { return true; } }, id); assert(invalid);
    await open('report.docx'); await region.getByText('DOCX_BODY_PROOF', { exact: true }).waitFor(); await image();
    assert.equal(await region.getByText('DOCX_BODY_PROOF', { exact: true }).evaluate(node => getComputedStyle(node).fontWeight), '700'); await shot('documents-word.png');
    await open('sheets.xlsx'); await region.getByRole('cell', { name: 'XLSX_PROOF', exact: true }).waitFor();
    await region.getByLabel('文档分区', { exact: true }).selectOption('1'); await region.getByRole('cell', { name: 'SECOND_SHEET', exact: true }).waitFor(); await shot('documents-excel.png');
    await open('slides.pptx'); await region.getByText('PPTX_PROOF', { exact: true }).waitFor(); await image();
    await region.getByLabel('文档分区', { exact: true }).selectOption('1'); await region.getByText('SECOND_SLIDE', { exact: true }).waitFor();
    await open('data.csv'); await region.getByRole('cell', { name: 'a,b', exact: true }).waitFor(); await region.getByRole('button', { name: '下一页', exact: true }).click(); await region.getByRole('cell', { name: 'sample48', exact: true }).waitFor();
    await open('data.tsv'); await region.getByRole('cell', { name: 'TSV_PROOF', exact: true }).waitFor();
    await open('data.json'); await region.locator('.document-code').filter({ hasText: 'JSON_PROOF' }).waitFor();
    await region.getByRole('tab', { name: '源码', exact: true }).click(); await region.locator('.file-source').filter({ hasText: 'JSON_PROOF' }).waitFor();
    fs.writeFileSync(path.join(project, 'data.json'), '{"proof":"REFRESHED_JSON"}');
    await region.getByRole('button', { name: '刷新文件内容', exact: true }).click(); await region.getByRole('tab', { name: '预览', exact: true }).click(); await region.locator('.document-code').filter({ hasText: 'REFRESHED_JSON' }).waitFor();
    await open('data.jsonl'); await region.locator('.document-code').filter({ hasText: '"value": 2' }).waitFor();
    await open('data.yaml'); await region.locator('.document-code').filter({ hasText: 'YAML_PROOF' }).waitFor();
    await open('data.xml'); await region.locator('.document-code').filter({ hasText: 'XML_PROOF' }).waitFor();
    await open('analysis.ipynb'); await region.getByRole('heading', { name: 'NOTEBOOK_PROOF', exact: true }).waitFor(); assert.equal(await page.evaluate(() => window.previewInjected), undefined);
    for (const suffix of ['odt', 'ods', 'odp']) { await open('open.' + suffix); await region.getByText(suffix.toUpperCase() + '_PROOF', { exact: true }).first().waitFor(); }
    await open('notes.rtf'); await region.locator('.document-code').filter({ hasText: 'RTF_PROOF' }).waitFor();
    await open('vector.svg'); await image();
    await open('scan.tiff'); await image(); await region.getByRole('button', { name: '下一页', exact: true }).click(); await region.getByAltText('文档第 2 页').waitFor(); await image();
    await open('icon.ico'); await image();
    await open('files.zip'); await region.getByRole('cell', { name: '../escape.txt', exact: true }).waitFor(); assert(!fs.existsSync(path.join(fixture, 'escape.txt')));
    await region.getByRole('button', { name: '关闭文件预览', exact: true }).click();
    assert.deepEqual(errors, []); console.log('Document E2E passed: 19 actual formats, PDF/TIFF pages and zoom, Word/images/table, Excel sheets, PowerPoint slides, structured text/source/refresh, notebook isolation, ODF, RTF, ZIP and workspace bounds');
  } finally { if (app) await app.close().catch(() => {}); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
