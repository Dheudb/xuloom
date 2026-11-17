// User-initiated, local maintenance. Never run provider calls or execute imported scripts.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');
const exec = promisify(execFile);

function createMaintenance({ app, dialog, shell, dataRoot, python, rpc, isBusy }) {
  let selected, pending, busy = false;
  const stateFile = path.join(dataRoot, 'maintenance-state.json');
  const read = () => { try { return JSON.parse(fs.readFileSync(stateFile, 'utf8')); } catch { return {}; } };
  function save(value) { fs.mkdirSync(dataRoot, {recursive:true}); const file = stateFile + '.tmp'; fs.writeFileSync(file, JSON.stringify({...read(), ...value})); fs.renameSync(file, stateFile); }
  async function run(module, args) {
    const { stdout } = await exec(python(), ['-X', 'utf8', '-m', module, ...args], { cwd:path.resolve(__dirname, '../..'), windowsHide:true, maxBuffer:1024*1024 });
    return JSON.parse(stdout);
  }
  async function handle(method) {
    if (!method.startsWith('app.maintenance.')) return {handled:false};
    const action = method.slice('app.maintenance.'.length);
    if (action === 'status') {
      const stored = read();
      let status = {};
      try { status = JSON.parse(fs.readFileSync(stored.status, 'utf8')); } catch {}
      return {handled:true, value:{...status, version:app.getVersion(), packaged:app.isPackaged,
        canImportSettings:fs.existsSync(path.join(dataRoot, 'restored-settings.json')),
        canRollback:!!stored.rollback && fs.existsSync(path.join(stored.rollback, 'Xuloom.exe')), pending:pending?.kind}};
    }
    if (action === 'releases') { await shell.openExternal('https://github.com/Dheudb/xuloom/releases/latest'); return {handled:true, value:{}}; }
    if (busy || isBusy()) throw new Error('请先停止正在运行的任务，再进行维护。');
    busy = true;
    try {
      let value = {};
      if (action === 'importSettings') value = await rpc('maintenance.importSettings', {});
      else if (action === 'backup' || action === 'diagnose') {
        const result = await dialog.showSaveDialog({ title:action === 'backup' ? 'Backup' : 'Diagnostics', defaultPath:action === 'backup' ? 'Xuloom-backup.zip' : 'Xuloom-diagnostics.json',
          filters:[{name:action === 'backup' ? 'ZIP' : 'JSON', extensions:[action === 'backup' ? 'zip' : 'json']}] });
        if (!result.canceled && result.filePath) value = await rpc('maintenance.' + action, {output:result.filePath});
      } else if (action === 'selectRestore') {
        const result = await dialog.showOpenDialog({properties:['openFile'], filters:[{name:'Xuloom backup', extensions:['zip']}]});
        if (!result.canceled && result.filePaths[0]) {
          value = await rpc('maintenance.inspect', {archive:result.filePaths[0]});
          const root = path.join(dataRoot, 'maintenance', crypto.randomUUID());
          fs.mkdirSync(root, {recursive:true});
          selected = path.join(root, 'backup.zip');
          fs.copyFileSync(result.filePaths[0], selected);
          pending = {kind:'restore', path:selected, status:path.join(root, 'status.json')};
          value = {...value, pending:'restore'};
        }
      } else if (action === 'selectUpdate' || action === 'rollback') {
        if (!app.isPackaged || process.platform !== 'win32') throw new Error('Windows 程序包中可用。');
        let args = ['--package', path.dirname(process.execPath)];
        if (action === 'rollback') {
          if (!read().rollback) throw new Error('没有可回退的程序副本。');
          args.push('--rollback', read().rollback);
        } else {
          const result = await dialog.showOpenDialog({properties:['openFile'], filters:[{name:'Xuloom Windows ZIP', extensions:['zip']}]});
          if (result.canceled) return {handled:true, value:{}};
          args.push('--archive', result.filePaths[0]);
        }
        const staged = await run('xuloom.maintenance.updates', args);
        pending = {kind:'update', ...staged, status:path.join(staged.stage, 'status.json')};
        // The helper comes from the current installation, never from the imported archive.
        fs.copyFileSync(path.join(__dirname, 'update-package.ps1'), path.join(staged.stage, 'apply.ps1'));
        value = {...staged, pending:'update'};
      } else if (action === 'apply') {
        if (!pending) throw new Error('请先选择并验证备份或程序包。');
        if (process.platform !== 'win32') throw new Error('Windows 程序包中可用。');
        const item = pending;
        let child;
        if (item.kind === 'restore') {
          child = spawn(python(), ['-X','utf8','-m','xuloom.maintenance.worker','--parent',String(process.pid),'--archive',item.path,'--data-root',dataRoot,'--status',item.status],
            {cwd:path.resolve(__dirname,'../..'), detached:true, windowsHide:true, stdio:'ignore'});
          save({status:item.status});
        } else {
          child = spawn('powershell.exe', ['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',path.join(item.stage,'apply.ps1'),'-Stage',item.stage],
            {cwd:item.stage, detached:true, windowsHide:true, stdio:'ignore'});
          save({status:item.status, rollback:item.rollback});
        }
        await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
        child.unref();
        if (item.kind === 'restore') {
          const until = Date.now() + 30000;
          while (!fs.existsSync(item.status) && Date.now() < until) await new Promise(resolve => setTimeout(resolve, 100));
          const started = JSON.parse(fs.readFileSync(item.status, 'utf8'));
          if (started.state !== 'waiting') throw new Error(started.error || 'Restore worker could not start');
        }
        pending = undefined;
        // Normal close path flushes drafts and waits for engines; no process is killed.
        setTimeout(() => app.quit(), 500);
        value = {waiting:true};
      } else throw new Error('Unknown maintenance action');
      return {handled:true, value};
    } finally { busy = false; }
  }
  return {handle};
}
module.exports = {createMaintenance};
