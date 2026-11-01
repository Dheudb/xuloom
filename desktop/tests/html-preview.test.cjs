const test = require('node:test'), assert = require('node:assert/strict');
const { HtmlPreviews } = require('../electron/html-preview.cjs');
function fixture() {
  const file = { path: '/remote/work/nested/page.html', asset: 'nested/page.html', mime: 'text/html; charset=utf-8', version: 'a'.repeat(64), size: 0 };
  const entries = new Map([['nested/page.html', Buffer.from('<h1>remote HTML</h1>')], ['style.css', Buffer.from('body {color:red}')], ['large.wasm', Buffer.alloc(700000, 31)], ['empty.txt', Buffer.alloc(0)]]);
  const calls = [], pages = new HtmlPreviews();
  const transport = { request: async (method, params) => {
    calls.push({ method, params });
    assert.equal(params.path, file.path);
    const asset = params.asset || file.asset, bytes = entries.get(asset);
    if (!bytes) throw new Error('denied');
    const info = { ...file, asset, size: bytes.length, mime: asset.endsWith('.css') ? 'text/css; charset=utf-8' : file.mime };
    if (method === 'files.htmlInfo') return info;
    assert.equal(method, 'files.htmlChunk'); assert.equal(params.version, info.version);
    const part = bytes.subarray(params.offset, params.offset + params.length);
    return { offset: params.offset, nextOffset: params.offset + part.length, size: info.size, version: info.version, data: part.toString('base64') };
  } };
  return { file, entries, calls, pages, transport };
}
test('HTML uses remote RPC, streams binary relative assets and keeps isolated origins', async () => {
  const f = fixture(), view = await f.pages.create('owner', f.transport, f.file.path);
  const another = await f.pages.create('other', f.transport, f.file.path);
  assert.notEqual(new URL(view.url).hostname, new URL(another.url).hostname);
  const request = relative => f.pages.respond(new Request(new URL(relative, view.url)));
  assert.match(await (await request(view.url)).text(), /remote HTML/);
  const css = await request('../style.css?v=1');
  assert.equal(css.headers.get('content-type'), 'text/css; charset=utf-8');
  assert.equal(css.headers.get('cache-control'), 'no-store');
  assert.match(css.headers.get('content-security-policy'), /form-action 'none'/);
  assert.match(await css.text(), /color:red/);
  f.entries.set('style.css', Buffer.from('body {color:blue}'));
  assert.match(await (await request('../style.css')).text(), /color:blue/);
  assert.equal(Buffer.from(await (await request('../large.wasm')).arrayBuffer()).length, 700000);
  assert(f.calls.filter(c => c.method === 'files.htmlChunk' && c.params.asset === 'large.wasm').length >= 3);
  assert(f.calls.filter(c => c.method === 'files.htmlChunk').every(c => c.params.length <= 262144));
  assert.equal(await (await request('../empty.txt')).text(), '');
});
test('HTML rejects traversal, hidden paths, writes and expired tokens before transport', async () => {
  const f = fixture(), view = await f.pages.create('owner', f.transport, f.file.path);
  const count = f.calls.length;
  for (const name of ['../.secret.json', '..%2f..%2foutside.html', '../a%5csecret.html'])
    assert.equal((await f.pages.respond(new Request(new URL(name, view.url)))).status, 403);
  assert.equal((await f.pages.respond(new Request(view.url, { method: 'POST' }))).status, 405);
  assert.equal(f.calls.length, count);
  f.pages.revoke(view.token, 'other');
  assert.equal((await f.pages.respond(new Request(view.url, { method: 'HEAD' }))).status, 200);
  assert.equal(f.calls.filter(c => c.method === 'files.htmlChunk').length, 0);
  f.pages.revokeSession('owner');
  assert.equal((await f.pages.respond(new Request(view.url))).status, 404);
});
test('HTML validates metadata and does not hide corrupt stream receipts or disconnects', async () => {
  for (const patch of [{asset:'../secret.html'}, {size:30*1024*1024+1}, {mime:'text/html\r\nX:evil'}, {version:'wrong'}]) {
    const f = fixture(), original = f.transport.request;
    f.transport.request = async (...args) => ({...await original(...args), ...patch});
    await assert.rejects(f.pages.create('owner', f.transport, f.file.path), /无法建立/);
  }
  const f = fixture(), view = await f.pages.create('owner', f.transport, f.file.path), original = f.transport.request;
  f.transport.request = async (method, params) => method === 'files.htmlChunk' ? {...await original(method, params), data:'!!!'} : original(method,params);
  await assert.rejects((await f.pages.respond(new Request(view.url))).arrayBuffer(), /回执无效/);
  f.transport.request = async () => {throw new Error('disconnected')};
  assert.equal((await f.pages.respond(new Request(view.url))).status, 404);
});
test('closing HTML during a pending chunk discards the response', async () => {
  const f = fixture(), view = await f.pages.create('owner', f.transport, f.file.path), original = f.transport.request;
  let release, started;
  const gate = new Promise(resolve => release=resolve), ready = new Promise(resolve => started=resolve);
  f.transport.request = async (method, params) => {if(method==='files.htmlChunk'){started();await gate;}return original(method,params)};
  const response = await f.pages.respond(new Request(view.url)), reading = response.arrayBuffer();
  await ready; f.pages.revokeSession('owner'); release();
  await assert.rejects(reading,/关闭/);
});
