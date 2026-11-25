const test = require('node:test');
const assert = require('node:assert/strict');
test('native RMB, mixed currencies, free models and legacy USD labels', async () => {
  const { formatCosts, usageText } = await import('../src/usage-cost.ts');
  assert.equal(formatCosts({CNY: 28.4}), '¥28.40');
  assert.equal(formatCosts({CNY: 0}), '¥0.00');
  assert.equal(formatCosts({USD: 2, CNY: 1}), '¥1.00 + $2.00');
  const base = {inputTokens: 10, outputTokens: 2, requests: 1, unpricedRequests: 0, estimated: false};
  assert.equal(usageText({...base, costAmounts: {CNY: 0.0012}, costUsd: 0}).text, '12 tokens · ¥0.0012');
  assert.equal(usageText({...base, costUsd: 0.1}).text, '12 tokens · $0.10');
  assert.equal(usageText({...base, costAmounts: {CNY: 1}, unpricedRequests: 1}).text, '12 tokens · ¥1.00 + 部分未计价');
  assert.equal(usageText({...base, costAmounts: {}, unpricedRequests: 1}).text, '12 tokens · 未计价');
  assert.equal(usageText({...base, requests: 0}), undefined);
});
