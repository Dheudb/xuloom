const { test } = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { buildInfo } = require('../electron/build-info.cjs');
test('build info reports matching packaged provenance and scoped validation without internal paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(),'xuloom-build-info-'));
  try {
    for (const [file, value] of Object.entries({ 'package.json':{version:'fixture',buildId:'build-fixture'},
      'build-provenance.json':{sourceCommit:'commit-fixture',dirty:false,sourceHashes:{'PRIVATE-PATH':'hash'},sourceTreeSha256:'source-hash'},
      'build-validation.json':{passed:true,scope:'local fixtures',summary:{python:1,skipped:0},realProvidersTested:false} })) fs.writeFileSync(path.join(root,file),JSON.stringify(value));
    const info=buildInfo(root); assert.equal(info.sourceCommit,'commit-fixture');assert.equal(info.dirty,false);
    assert.equal(info.validation.present,true);assert.equal(info.validation.realProvidersTested,false);
    assert(!JSON.stringify(info).includes('PRIVATE-PATH'));
    fs.unlinkSync(path.join(root,'build-validation.json'));assert.equal(buildInfo(root).validation.present,false);
  } finally { fs.rmSync(root,{recursive:true,force:true}); }
});
