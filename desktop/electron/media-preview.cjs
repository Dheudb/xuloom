const { randomUUID } = require("node:crypto");
const { previewStream } = require("./preview-stream.cjs");
const SCHEME = "xuloom-media";

class MediaPreviews {
  constructor() { this.pages = new Map(); }
  async create(session, transport, path) {
    const file = await transport.request("files.mediaInfo", { path });
    if (!new Set(["video/mp4", "video/webm", "audio/mpeg", "audio/wav", "audio/flac", "audio/ogg", "audio/mp4", "audio/webm"]).has(file.mime) ||
        typeof file.path !== "string" || !/^[a-f0-9]{64}$/.test(file.version) || !Number.isSafeInteger(file.size) || file.size <= 0 || file.size > 256 * 1024 * 1024)
      throw new Error("请选择可预览的音视频文件（最多 256 MB）");
    const token = randomUUID();
    this.pages.set(token, { session, transport, file });
    return { token, url: `${SCHEME}://${token}/content`, mime: file.mime, size: file.size };
  }
  revoke(token, session) { if (this.pages.get(token)?.session === session) this.pages.delete(token); }
  revokeSession(session) { for (const [token, page] of this.pages) if (page.session === session) this.pages.delete(token); }
  async respond(request) {
    const denied = (status, message, headers = {}) => new Response(message, { status, headers: { "content-type": "text/plain; charset=utf-8", ...headers } });
    try {
      const url = new URL(request.url), page = this.pages.get(url.hostname);
      if (url.protocol !== SCHEME + ":" || url.pathname !== "/content" || url.search || url.hash || url.username || url.password || url.port || !page)
        return denied(404, "媒体预览已关闭");
      if (!["GET", "HEAD"].includes(request.method)) return denied(405, "媒体预览仅支持读取");
      const { file, transport } = page;
      let start = 0, end = file.size - 1;
      const range = request.headers.get("range");
      if (range) {
        const match = /^bytes=(\d*)-(\d*)$/.exec(range);
        if (!match || !match[1] && !match[2]) return denied(416, "无效媒体范围", { "content-range": `bytes */${file.size}` });
        if (!match[1]) { const suffix = Number(match[2]); if (suffix <= 0 || !Number.isSafeInteger(suffix)) return denied(416, "无效媒体范围"); start = Math.max(0, file.size - suffix); }
        else { start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), end) : end; }
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= file.size)
          return denied(416, "媒体范围超出文件", { "content-range": `bytes */${file.size}` });
      }
      const body = request.method === "HEAD" ? null : previewStream(transport, "files.mediaChunk", { path: file.path }, file,
        start, end, () => this.pages.has(url.hostname));
      return new Response(body, { status: range ? 206 : 200, headers: {
        "content-type": file.mime, "content-length": String(end - start + 1), "accept-ranges": "bytes",
        ...(range ? { "content-range": `bytes ${start}-${end}/${file.size}` } : {}),
        "cache-control": "no-store", "x-content-type-options": "nosniff", "access-control-allow-origin": "*",
      } });
    } catch { return denied(404, "无法读取媒体，请重新打开文件"); }
  }
}
module.exports = { MediaPreviews, SCHEME };
