const test = require('node:test');
const assert = require('node:assert/strict');
const { updateLayout } = require('../electron/layout.cjs');

test('saving either panel width preserves the other preference', () => {
  const previous = { inspectorWidth: 560, sidebarWidth: 300 };
  assert.deepEqual(updateLayout(previous, { sidebarWidth: 380 }), { inspectorWidth: 560, sidebarWidth: 380 });
  assert.deepEqual(updateLayout(previous, { inspectorWidth: 440 }), { inspectorWidth: 440, sidebarWidth: 300 });
  assert.deepEqual(previous, { inspectorWidth: 560, sidebarWidth: 300 });
  assert.deepEqual(updateLayout(undefined, { sidebarWidth: 300.4 }), { sidebarWidth: 300 });
  assert.equal(updateLayout(previous, { inspectorWidth: 1000 }).inspectorWidth, 1000);
  assert.equal(updateLayout(previous, { reviewTreeWidth: 180 }).reviewTreeWidth, 180);
  assert.deepEqual(updateLayout(previous, { filePreviewWidth: 760 }), { ...previous, filePreviewWidth: 760 });
  for (const patch of [{}, { sidebarWidth: 100 }, { sidebarWidth: Infinity }, { inspectorWidth: 13000 }, { reviewTreeWidth: 50 }, { sidebarWidth: '300' }])
    assert.throws(() => updateLayout(previous, patch));
});

test('both panels respect each other and retain usable conversation space', async () => {
  const { clampPanelWidth, clampSidebarWidth } = await import('../src/panel-width.ts');
  assert.equal(clampSidebarWidth(404, 1050), 370);
  assert.equal(clampSidebarWidth(404, 1050, false), 404);
  for (const viewport of [1000, 1050, 1100, 1440, 1600, 2200]) {
    const sidebar = clampSidebarWidth(480, viewport);
    const inspector = clampPanelWidth(760, viewport, sidebar);
    assert(viewport - sidebar - inspector >= 400);
  }
});

test('review tree leaves readable file space and wide screens are not capped at 760', async () => {
  const { clampPanelWidth, clampReviewTreeWidth } = await import('../src/panel-width.ts');
  assert.equal(clampPanelWidth(1100, 2200, 300), 1100);
  assert.equal(clampReviewTreeWidth(220, 280), 120);
  assert.equal(clampReviewTreeWidth(900, 1000), 820);
});
