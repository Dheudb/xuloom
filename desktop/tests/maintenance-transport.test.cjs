const test = require('node:test'), assert = require('node:assert/strict');
const { EngineTransport } = require('../electron/transport.cjs');

test('archive operations do not time out as ordinary one-minute metadata requests', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  for (const method of ['maintenance.backup', 'maintenance.inspect']) {
    const transport = Object.assign(Object.create(EngineTransport.prototype), {
      closed:false, sequence:0, pending:new Map(), child:{stdin:{write(){}}},
    });
    let rejected = false;
    const response = transport.request(method).catch(() => { rejected = true; });
    t.mock.timers.tick(61000);
    await Promise.resolve();
    assert.equal(rejected, false, method);
    transport.pending.get(1).resolve({ok:true});
    await response;
  }
});
