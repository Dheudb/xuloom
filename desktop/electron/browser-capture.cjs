// One-shot Chromium rendering, separate from the user's window and login profile.
const { BrowserWindow, session } = require("electron");
const fs = require("node:fs/promises"), path = require("node:path");
const { randomUUID } = require("node:crypto");
const { HtmlPreviews, SCHEME } = require("./html-preview.cjs");

function inside(root, file) {
  const relative = path.relative(root, file);
  return relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
}
class BrowserCaptures {
  constructor() { this.active = new Map(); }
  cancel(owner, requestId) {
    for (const [key, entry] of this.active) if (entry.owner === owner && (!requestId || key === owner + ":" + requestId)) entry.cancel();
  }
  async capture(owner, project, requestId, input, transport) {
    const key = owner + ":" + requestId;
    if (this.active.has(key)) throw new Error("Duplicate capture request");
    if (this.active.size >= 4) throw new Error("Four screenshot renders are already running; retry after they finish");
    const { width = 1440, height = 960, wait_ms = 500, full_page = true } = input;
    if (![width, height, wait_ms].every(Number.isInteger) || width < 320 || width > 2560 || height < 240 || height > 2160 || wait_ms < 0 || wait_ms > 10000)
      throw new Error("Invalid screenshot viewport or wait_ms");
    if (Boolean(input.file_path) === Boolean(input.url)) throw new Error("Provide one HTML file or HTTP(S) URL");
    const previews = new HtmlPreviews();
    let win, browserSession, stopped = false, rejectCancelled;
    const cancelled = new Promise((_resolve, reject) => { rejectCancelled = reject; });
    const cancel = message => { stopped = true; rejectCancelled(new Error(message)); };
    this.active.set(key, { owner, cancel() { cancel("Screenshot cancelled"); } });
    const timer = setTimeout(() => cancel("Screenshot timed out after 40 seconds"), 40000);
    try {
      return await Promise.race([cancelled, (async () => {
        let target;
        if (input.file_path) {
          const root = await fs.realpath(project), file = await fs.realpath(path.resolve(root, input.file_path));
          if (!inside(root, file) || path.relative(root, file).split(path.sep).some(part => part.startsWith(".")))
            throw new Error("Screenshot HTML must be inside the session workspace, outside hidden directories");
          if (!transport?.request) throw new Error("Screenshot HTML requires its owning engine's file transport");
          target = (await previews.create(owner, transport, file)).url;
        } else {
          const url = new URL(input.url);
          if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Only HTTP(S) URLs without credentials are allowed");
          target = url.href;
        }
        if (stopped) throw new Error("Screenshot cancelled");
        browserSession = session.fromPartition("capture-" + randomUUID()); // Memory only, no user cookies.
        browserSession.setPermissionRequestHandler((_web, _permission, callback) => callback(false));
        browserSession.setPermissionCheckHandler(() => false);
        browserSession.on("will-download", event => event.preventDefault());
        browserSession.protocol.handle(SCHEME, request => previews.respond(request));
        const previewHost = new URL(target).protocol === SCHEME + ":" ? new URL(target).host : null;
        let httpError;
        browserSession.webRequest.onHeadersReceived((details, callback) => {
          if (details.resourceType === "mainFrame" && details.statusCode >= 400) httpError = "Page returned HTTP " + details.statusCode;
          callback({});
        });
        browserSession.webRequest.onBeforeRequest((details, callback) => {
          let allowed = false;
          try {
            const url = new URL(details.url);
            allowed = ["http:", "https:", "data:", "blob:", "about:"].includes(url.protocol) || url.protocol === SCHEME + ":" && url.host === previewHost;
          } catch {}
          callback({ cancel: !allowed });
        });
        win = new BrowserWindow({ show: false, width, height, useContentSize: true, enableLargerThanScreen: true,
          webPreferences: { session: browserSession, sandbox: true, contextIsolation: true, nodeIntegration: false,
            webSecurity: true, backgroundThrottling: false, spellcheck: false } });
        win.setContentSize(width, height);
        win.webContents.setZoomFactor(1);
        win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
        win.webContents.on("will-navigate", event => event.preventDefault());
        const problems = [];
        win.webContents.on("console-message", (_event, details, message) => {
          // Support both the bundled runtime and newer source-test Electron.
          const error = typeof details === "object" ? details.level === "error" || details.level === 3 : details === 3;
          if (error && problems.length < 10) problems.push(String(typeof details === "object" ? details.message : message).slice(0, 500));
        });
        await win.loadURL(target);
        if (httpError) throw new Error(httpError);
        await win.webContents.executeJavaScript(`Promise.race([document.fonts.ready.then(()=>true),new Promise(r=>setTimeout(r,1000))])`);
        await new Promise(resolve => setTimeout(resolve, wait_ms));
        const size = await win.webContents.executeJavaScript("({width:Math.max(innerWidth,document.documentElement.scrollWidth),height:Math.max(innerHeight,document.documentElement.scrollHeight)})");
        const truncated = Boolean(full_page && (size.height > 8192 || size.width > 4096 || size.width * size.height > 16000000));
        if (full_page && !truncated) win.setContentSize(Math.ceil(size.width), Math.ceil(size.height));
        // Wake the compositor without ever showing or focusing the hidden window.
        await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
        await new Promise(resolve => setTimeout(resolve, 100));
        const captured = await win.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
        // Native capture uses the monitor's device scale (e.g. Windows 125%).
        // Return CSS-pixel dimensions consistently across monitors and platforms.
        const image = captured.resize({ width: full_page && !truncated ? Math.ceil(size.width) : width,
          height: full_page && !truncated ? Math.ceil(size.height) : height, quality: "best" });
        const bytes = image.toPNG();
        if (image.isEmpty() || bytes.length > 20 * 1024 * 1024) throw new Error("Screenshot is empty or exceeds 20 MB");
        return { png_base64: bytes.toString("base64"), title: win.webContents.getTitle(), url: input.file_path || win.webContents.getURL(),
          ...image.getSize(), truncated, console_errors: problems, backend: "electron-hidden" };
      })()]);
    } finally {
      stopped = true;
      clearTimeout(timer);
      this.active.delete(key);
      if (win && !win.isDestroyed()) win.destroy();
      previews.revokeSession(owner);
      if (browserSession) {
        browserSession.protocol.unhandle(SCHEME);
        browserSession.webRequest.onBeforeRequest(null);
        browserSession.webRequest.onHeadersReceived(null);
        await browserSession.clearStorageData();
        await browserSession.closeAllConnections();
      }
    }
  }
}
module.exports = { BrowserCaptures };
