function updateLayout(previous = {}, patch = {}) {
  const layout = { ...previous };
  let changed = false;
  for (const [key, min, max, label] of [
    ['inspectorWidth', 280, 12000, '右侧面板'], ['sidebarWidth', 200, 480, '历史栏'], ['reviewTreeWidth', 120, 12000, '文件目录栏'],
    ['filePreviewWidth', 280, 12000, '文件预览'],
  ]) {
    if (!(key in patch)) continue;
    if (!Number.isFinite(patch[key]) || patch[key] < min || patch[key] > max)
      throw new Error(`${label}宽度必须在 ${min} 到 ${max} 之间`);
    layout[key] = Math.round(patch[key]); changed = true;
  }
  if (!changed) throw new Error('请选择需要调整的面板宽度');
  return layout;
}
module.exports = { updateLayout };
