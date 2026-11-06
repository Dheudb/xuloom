const fs = require('node:fs'), path = require('node:path');
function read(file) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return {}; } }
function buildInfo(directory) {
  const pkg = read(path.join(directory, 'package.json'));
  const source = read(path.join(directory, 'build-provenance.json'));
  const validation = read(path.join(directory, 'build-validation.json'));
  return { version: pkg.version || '', buildId: pkg.buildId || '', sourceCommit: source.sourceCommit || '',
    sourceTreeSha256: source.sourceTreeSha256 || '', dirty: source.dirty ?? null,
    validation: { present: validation.passed === true, scope: validation.scope || '', checks: validation.summary || {},
      realProvidersTested: validation.realProvidersTested === true } };
}
module.exports = { buildInfo };
