const { randomUUID } = require("node:crypto");
const { previewStream } = require("./preview-stream.cjs");
const SCHEME = "xuloom-preview";
const CSP = "default-src 'self' data: blob: https: http:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https: http:; style-src 'self' 'unsafe-inline' https: http:; object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'none'";
function valid(file) {
  return file && typeof file.path === "string" && file.path.length && typeof file.asset === "string" && file.asset.length &&
    !/[\0\\:]/.test(file.asset) && !file.asset.startsWith("/") && !file.asset.split("/").some(part => !part || part.startsWith(".")) &&
    typeof file.mime === "string" && /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/.test(file.mime) &&
    /^[a-f0-9]{64}$/.test(file.version) && Number.isSafeInteger(file.size) && file.size >= 0 && file.size <= 30 * 1024 * 1024;
}
class HtmlPreviews {
  constructor() { this.pages = new Map(); }
  async create(session, transport, path) {
    const file = await transport.request("files.htmlInfo", { path });
    if (!valid(file) || file.mime !== "text/html; charset=utf-8") throw new Error("无法建立网页预览，请检查文件并更新执行端 Xuloom");
    const token = randomUUID();
    this.pages.set(token, { session, transport, file });
    return { token, url: `${SCHEME}://${token}/${file.asset.split("/").map(encodeURIComponent).join("/")}` };
  }
  revoke(token, session) { if (this.pages.get(token)?.session === session) this.pages.delete(token); }
  revokeSession(session) { for (const [token, page] of this.pages) if (page.session === session) this.pages.delete(token); }
  async respond(request) {
    const denied = (status, message) => new Response(message, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
    try {
      const url = new URL(request.url), page = this.pages.get(url.hostname);
      if (url.protocol !== SCHEME + ":" || url.username || url.password || url.port || !page) return denied(404, "预览已关闭，请重新打开文件。");
      if (!["GET", "HEAD"].includes(request.method)) return denied(405, "预览仅支持读取文件。");
      const asset = decodeURIComponent(url.pathname).replace(/^\/+/, "");
      if (!asset || /[\0\\:]/.test(asset) || asset.split("/").some(part => !part || part.startsWith("."))) return denied(403, "无法读取此资源。");
      const params = { path: page.file.path, asset };
      const file = await page.transport.request("files.htmlInfo", params);
      if (!this.pages.has(url.hostname)) return denied(404, "预览已关闭");
      if (!valid(file) || file.path !== page.file.path || file.asset !== asset) return denied(502, "网页资源回执无效");
      return new Response(request.method === "HEAD" ? null : previewStream(page.transport, "files.htmlChunk", params, file,
        0, file.size - 1, () => this.pages.has(url.hostname)), { headers: {
        "content-type": file.mime, "content-length": String(file.size), "cache-control": "no-store",
        "x-content-type-options": "nosniff", "content-security-policy": CSP,
      } });
    } catch { return denied(404, "文件已移动、删除，连接已断开或资源不可读取。"); }
  }
}
module.exports = { HtmlPreviews, SCHEME };
