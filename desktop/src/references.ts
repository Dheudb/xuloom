export type FileReference = { path: string; line?: number; column?: number; basePath?: string };
const controls = /[\u0000-\u001f\u007f]/;
export function webReference(value: string): string | undefined {
  if (controls.test(value)) return;
  try { const u = new URL(value); if (["http:", "https:"].includes(u.protocol) && !u.username && !u.password) return u.href; } catch { /* Not a web address. */ }
}
export function fileReference(value: string, inline = false): FileReference | undefined {
  if (!value || value.length > 4096 || controls.test(value) || /[<>]/.test(value)) return;
  if (/^(?:https?|javascript|data|file|mailto|ssh|vscode|xuloom):/i.test(value) || /^[a-z]:[^\\/]/i.test(value)) return;
  let hash = value.indexOf("#"), destination = hash < 0 ? value : value.slice(0, hash);
  if (destination.includes("?")) return;
  const suffix = hash < 0 ? /:(\d+)(?::(\d+)|-(\d+))?$/.exec(destination) : null;
  if (suffix) destination = destination.slice(0, -suffix[0].length);
  let path: string;
  try { if (/%(?![\da-f]{2})/i.test(destination)) return; path = decodeURIComponent(destination); } catch { return; }
  if (!path || controls.test(path) || path.startsWith("\\\\") || path.startsWith("//") ||
    (/^[a-z][\w+.-]*:/i.test(path) && !/^[a-z]:[\\/]/i.test(path))) return;
  if (inline && !/^(?:[\\/]|\.{1,2}[\\/]|[a-z]:[\\/])/i.test(path) && !/[\\/]/.test(path) && !/^(?:[^:]+\.[a-z\d]{1,12}|Dockerfile|Makefile|LICENSE|README|Procfile)$/i.test(path)) return;
  const location = suffix || (hash < 0 ? null : /^#L(\d+)(?:-L(\d+))?$/.exec(value.slice(hash)));
  const line = location ? Number(location[1]) : 0;
  const end = suffix?.[3] || (!suffix ? location?.[2] : undefined);
  if (Number.isSafeInteger(line) && line > 0 && line <= 10000000 && (!end || Number(end) >= line && Number(end) <= 10000000)) {
    const column = suffix?.[2] ? Number(suffix[2]) : undefined;
    return { path, line, ...(column && Number.isSafeInteger(column) && column <= 10000000 ? { column } : {}) };
  }
  return { path };
}
