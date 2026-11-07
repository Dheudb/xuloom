const test = require('node:test'), assert = require('node:assert/strict');
async function renderTree(text) {
  const [{ unified }, { default: parse }, { default: gfm }, { default: math }, { default: references }] = await Promise.all([
    import('unified'), import('remark-parse'), import('remark-gfm'), import('remark-math'), import('../src/remark-reference-list.ts')]);
  const processor = unified().use(parse).use(gfm).use(math).use(references);
  return processor.run(processor.parse(text));
}
test('compact source paragraphs become ordered entries while retaining rich titles and actual URLs', async () => {
  const tree = await renderTree('Research statement [1].\n\n---\n\n[1] Author, "First paper" [2] **Second paper**: https://example.org/paper?q=one#abstract [3] [Third [2] title](https://example.org/third)');
  assert.equal(tree.children[0].type, 'paragraph');
  const list = tree.children[2];
  assert.equal(list.type, 'list'); assert.equal(list.children.length, 3);
  assert.equal(list.data.hProperties.ariaLabel, '参考来源');
  assert.equal(list.children[1].children[0].children[0].type, 'strong');
  assert.equal(list.children[1].children[0].children.find(n=>n.type==='link').url, 'https://example.org/paper?q=one#abstract');
  assert.equal(list.children[2].children[0].children[0].children[0].value, 'Third [2] title');
});
test('source headings, multiline paragraphs and unheaded linked bibliographies are supported', async () => {
  for (const source of ['## 参考文献\n\n[1] First paper\n[2] Second paper', '[1] [First](https://example.org/one) [2] Second paper']) {
    const tree = await renderTree(source), list = tree.children.at(-1);
    assert.equal(list.type, 'list'); assert.equal(list.children.length, 2);
  }
});
test('inline citations, existing lists, code, math, incomplete and nonsequential references remain untouched', async () => {
  const examples = [
    'Evidence [1] and [2] is discussed here.',
    '[1] and [2] are labels, not a bibliography.',
    '[1] https://example.org/one [3] third source',
    '[1] https://example.org/one [2] ',
    '```text\n[1] https://example.org/one [2] second\n```',
    '$[1] + [2]$',
    '1. [1] https://example.org/one [2] second',
    '[1]: https://example.org/one\n[2]: https://example.org/two',
  ];
  for (const source of examples) {
    const tree = await renderTree(source);
    assert(!tree.children.some(node => node.data?.hProperties?.className?.includes('reference-list')), source);
  }
});
