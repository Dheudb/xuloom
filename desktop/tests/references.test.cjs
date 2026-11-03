const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
test('desktop reference parser follows the shared CLI contract', async () => {
  const { fileReference, webReference } = await import('../src/references.ts');
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, '../../tests/fixtures/reference-cases.json')));
  for (const item of cases) assert.deepEqual(fileReference(item.value, item.inline) || null, item.expected, item.value);
  for (const url of ['https://github.com/a/b', 'http://localhost:8000/a']) assert(webReference(url));
  for (const url of ['javascript:42', 'file:///a', 'https://user:key@example.org', 'https://example.org\n']) assert.equal(webReference(url), undefined);
});
