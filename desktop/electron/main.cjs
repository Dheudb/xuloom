const { app, BrowserWindow, ipcMain, dialog, shell, protocol, nativeImage, clipboard } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { SessionCatalog, projectKey, sessionKey } = require("./catalog.cjs");
const { EngineTransport } = require("./transport.cjs");
const { resolveEnginePython } = require("./runtime.cjs");
const { defaultWorkspace, dataDirectory } = require("./workspace.cjs");
const preferences = require("./settings.cjs");
const { parseLink, chatLink, startupLinks } = require("./chat-links.cjs");
const { prepareMove, moveSpec } = require("./session-move.cjs");
const { HtmlPreviews, SCHEME: PREVIEW_SCHEME } = require("./html-preview.cjs");
protocol.registerSchemesAsPrivileged([{ scheme: PREVIEW_SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } }]);
const htmlPreviews = new HtmlPreviews();
const { BrowserCaptures } = require("./browser-capture.cjs");
const browserCaptures = new BrowserCaptures();
app.setName("Xuloom");
if (process.env.XULOOM_USER_DATA)
  app.setPath("userData", process.env.XULOOM_USER_DATA);
const requestedDataRoot = process.env.XULOOM_DATA_DIR || process.env.XULOOM_USER_DATA;
const dataRoot = requestedDataRoot && path.isAbsolute(requestedDataRoot) ? path.resolve(requestedDataRoot) : dataDirectory({ home: app.getPath("home") });
process.env.XULOOM_DATA_DIR = dataRoot;
const sessions = new Map();
let window;
let uiReady = false;
const pendingLinks = startupLinks(process.argv);
// Normal secondary workbenches remain allowed. Only URL launches forward to
// the primary instance, avoiding another writer and another empty window.
const primaryInstance = app.requestSingleInstanceLock({ chatLinks: pendingLinks });
const forwardedLink = !primaryInstance && pendingLinks.length > 0;
if (forwardedLink) app.quit();
function deliverLinks() {
  if (!uiReady || !window || window.isDestroyed()) return;
  for (const url of pendingLinks.splice(0)) window.webContents.send("workbench:event", { type: "chat_link", url });
  if (!process.env.XULOOM_TEST_HEADLESS) {
    if (window.isMinimized()) window.restore();
    window.show(); window.focus();
  }
}
function receiveLinks(values) {
  pendingLinks.push(...values.slice(0, Math.max(0, 16 - pendingLinks.length)));
  deliverLinks();
}
app.on("second-instance", (_event, argv, _cwd, extra) => {
  const links = Array.isArray(extra?.chatLinks) ? startupLinks(extra.chatLinks) : startupLinks(argv);
  if (links.length) receiveLinks(links);
});
app.on("open-url", (event, url) => { event.preventDefault(); receiveLinks(startupLinks([url])); });
let quitting = false;
let configTransport;
let activeSessionId;
let restoration;
const catalog = new SessionCatalog(settings, saveSettings, dataRoot);
const { Attachments } = require("./attachments.cjs");
const attachments = new Attachments(catalog, nativeImage);
let workspaceQueue = Promise.resolve();
let catalogRevision = 0;
const engineMethods = new Set([
  "collaboration.get", "collaboration.set", "agents.resume_queue", "execution.get", "execution.set",
  "goal.get", "goal.create", "goal.run", "goal.control", "goal.update",
  "schedule.list", "schedule.add", "schedule.control",
  "prompt",
  "prompt.resume",
  "respond",
  "stop",
  "changes",
  "files.read",
  "files.image",
  "media.read",
  "files.undo",
  "files.list",
  "git.status",
  "files.gitDiff",
  "capabilities.list",
  "capabilities.toggle",
  "skills.read",
  "agents.read",
  "agents.cancel",
  "agents.list",
  "agents.capacity",
  "config.save",
  "config.select",
  "config.reasoning",
  "config.delete",
  "mcp.list",
  "mcp.market",
  "mcp.install",
  "mcp.save",
  "mcp.toggle",
  "mcp.remove",
  "mcp.test",
  "mcp.reload",
  "capabilities.example",
  "capabilities.create",
]);
function settings() {
  return preferences.readSettings(dataRoot, path.join(app.getPath("userData"), "workbench.json"));
}
function saveSettings(value, before) {
  return preferences.saveSettings(dataRoot, value, before);
}
function workspaceSettings() {
  const saved = settings().defaultDirectory;
  return { defaultDirectory: typeof saved === "string" && path.isAbsolute(saved)
    ? saved : defaultWorkspace({ home: app.getPath("home") }) };
}
function ensureWorkspace(directory) {
  if (typeof directory !== "string" || !path.isAbsolute(directory.trim()))
    throw new Error("默认工作目录必须是完整的绝对路径");
  directory = directory.trim();
  fs.mkdirSync(directory, { recursive: true });
  if (!fs.statSync(directory).isDirectory()) throw new Error("请选择一个目录");
  return fs.realpathSync(directory);
}
function settingsFailure(error, prefix = "工作台设置更新失败，请检查后重新选择会话。") {
  if (window && !window.isDestroyed()) window.webContents.send("workbench:event", {
    type: "settings_error", message: prefix + error.message,
  });
}
function broadcastCatalog() {
  const snapshot = { ...catalog.snapshot(), revision: ++catalogRevision };
  if (window && !window.isDestroyed())
    window.webContents.send("workbench:event", { type: "catalog_changed", catalog: snapshot });
  return snapshot;
}
function send(id, payload) {
  const source = sessions.get(id);
  // Host requests never enter the frontend or its generic respond channel.
  if (payload.type === "browser_capture") {
    if (source) browserCaptures.capture(id, source.project, payload.requestId, payload.input)
      .catch(error => ({ error: error.message }))
      .then(result => source.transport.request("browser.capture_result", { requestId: payload.requestId, result }))
      .catch(() => {}); // A cancelled/closed engine no longer owns a pending request.
    return;
  }
  if (payload.type === "browser_capture_cancel") { browserCaptures.cancel(id, payload.requestId); return; }
  if (payload.type === "engine_exit") browserCaptures.cancel(id);
  if (source && payload.type === "managed_turn_started") source.busy = true;
  if (source && payload.type === "turn_finished") source.busy = false;
  if (source && payload.type === "subagent_status") source.workers.set(payload.agent.agent_id, payload.agent.status);
  if (source && payload.type === "command_status") {
    source.commands ||= new Map();
    source.commands.set(payload.command.id, payload.command.status);
  }
  if (payload.type === "session_started" && sessions.has(id)) {
    sessions.get(id).resumeId = payload.sessionId;
    try { saveWorkspace(); }
    catch (error) { settingsFailure(error); }
  }
  if (window && !window.isDestroyed())
    window.webContents.send("workbench:event", { session: id, ...payload });
  if (payload.type === "engine_exit") sessions.delete(id);
  if (["session_started", "sessions_changed"].includes(payload.type)) {
    try { broadcastCatalog(); }
    catch (error) { settingsFailure(error); }
  }
}
let tickingSchedules = false;
async function tickSchedules() {
  if (tickingSchedules || quitting || !uiReady || !restoration || !fs.existsSync(path.join(dataRoot, "schedules.json"))) return;
  tickingSchedules = true;
  try {
    await restoration;
    const due = await globalConfig().request("schedule.due");
    for (const job of due) {
      if (quitting) break;
      let row;
      try { row = catalog.find(sessionKey(job.cwd, job.session_id)); }
      catch {
        await globalConfig().request("schedule.control", { id: job.id, action: "pause" });
        settingsFailure(new Error("定时消息的会话已移动或删除，任务已暂停，请检查后重新创建。"), "");
        continue;
      }
      if (row.archived) continue;
      const source = sessions.get(row.id);
      if (source && running(source)) continue;
      try {
        await createSession(row.project, row.sessionId);
        await sessions.get(row.id).transport.request("schedule.deliver", { id: job.id });
      } catch (error) {
        // A CLI owner may be holding the session. The durable due message stays queued.
        if (!String(error.message).includes("writer") && !String(error.message).includes("占用")) settingsFailure(error, "定时消息等待投递：");
      }
    }
  } catch (error) { settingsFailure(error, "定时消息检查失败："); }
  finally { tickingSchedules = false; }
}
function saveWorkspace() {
  const active = sessions.get(activeSessionId);
  const before = settings();
  saveSettings({
    ...before,
    activeConversation: active ? { project: active.project, resumeId: active.resumeId, scope: active.scope } : null,
    workspace: [...sessions.entries()].map(([id, session]) => ({
      project: session.project,
      resumeId: session.resumeId,
      scope: session.scope,
      active: id === activeSessionId,
    })),
  }, before);
}
function latestSavedSession(project, scope = "project") {
  try {
    return catalog.list(project).filter((row) => row.scope === scope && !row.archived && !row.error)
      .sort((a, b) => b.updated - a.updated)[0]?.sessionId;
  } catch {
    return undefined;
  }
}
async function restoreWorkspace() {
  const upgradeErrors = [];
  try { await catalog.upgradeStandaloneWorkspaces(workspaceSettings().defaultDirectory, mutateSessions); }
  catch (error) { upgradeErrors.push("旧独立会话的工作区隔离未完成，历史和原文件已保留：" + error.message); }
  const saved = settings();
  const legacyEntries = Array.isArray(saved.workspace) ? saved.workspace :
    (saved.projects || []).map((project) => ({ project, resumeId: latestSavedSession(project) }))
      .filter((entry) => entry.resumeId).slice(0, 1);
  const selected = Object.prototype.hasOwnProperty.call(saved, "activeConversation") ? saved.activeConversation :
    legacyEntries.find((entry) => entry.active) || legacyEntries[0];
  const entries = selected ? [selected] : [];
  const restored = [], errors = [...upgradeErrors];
  for (const entry of entries.slice(0, 8)) {
    try {
      if (entry.scope !== "standalone" && catalog.snapshot().projects.some((project) => projectKey(project.path) === projectKey(entry.project) && project.archived)) continue;
      let resumeId = entry.resumeId;
      if (resumeId && (!/^[a-zA-Z0-9_-]+$/.test(resumeId) ||
        !fs.existsSync(catalog.file(entry.project, resumeId))))
        resumeId = latestSavedSession(entry.project, entry.scope || "project");
      if (resumeId && catalog.list(entry.project).find((row) => row.sessionId === resumeId)?.archived)
        resumeId = latestSavedSession(entry.project, entry.scope || "project");
      if (!resumeId && entry.scope === "standalone") continue;
      const initial = await createSession(entry.project, resumeId);
      restored.push(initial);
      activeSessionId = initial.id;
    } catch {
      errors.push("无法恢复项目：" + entry.project);
    }
  }
  activeSessionId ||= restored[0]?.id;
  return { restored, activeId: activeSessionId, restoreErrors: errors };
}
function engineRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "engine")
    : path.resolve(__dirname, "../..");
}
function globalConfig() {
  if (!configTransport || configTransport.closed) {
    const directory = app.getPath("userData");
    fs.mkdirSync(directory, { recursive: true });
    configTransport = new EngineTransport({
      python: resolveEnginePython({
        packaged: app.isPackaged,
        resourcesPath: process.resourcesPath,
        projectRoot: path.resolve(__dirname, "../.."),
      }),
      isolated: app.isPackaged,
      engineRoot: engineRoot(),
      project: directory,
      onEvent: () => {},
    });
  }
  return configTransport;
}
async function createSession(project, resumeId) {
  project = fs.realpathSync(project);
  if (!fs.statSync(project).isDirectory()) throw new Error("项目目录不存在");
  const row = resumeId ? catalog.find(sessionKey(project, resumeId)) : catalog.create(project);
  resumeId = row.sessionId;
  const id = row.id;
  if (sessions.has(id)) {
    const previous = sessions.get(id).transport;
    if (previous.closed || previous.closeError) await disposeSession(id);
    else return { existingId: id };
  }
  if (sessions.size >= 8) {
    const idle = [...sessions.entries()].find(([key, source]) => key !== activeSessionId && !running(source));
    if (!idle) throw new Error("已有 8 个运行中的会话，请先停止一个任务");
    await disposeSession(idle[0]);
  }
  const transport = new EngineTransport({
    python: resolveEnginePython({
      packaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      projectRoot: path.resolve(__dirname, "../.."),
    }),
    isolated: app.isPackaged,
    engineRoot: engineRoot(),
    project,
    onEvent: (event) => send(id, event),
  });
  sessions.set(id, { transport, project, resumeId, scope: row.scope, busy: false, workers: new Map() });
  try {
    const initial = await transport.request("initialize", {
      sessionId: resumeId,
      permissionMode: row.permissionMode,
    });
    const value = { id, ...initial, scope: row.scope, title: row.title, savedDraft: row.draft || "", savedDraftEditId: row.draftEditId || "", savedAttachments: attachments.list(row, row.draftAttachments || []) };
    // Background schedule/navigation paths can load a transport before the
    // renderer knows the chat. Announce it before any subsequent turn events.
    if (uiReady && window && !window.isDestroyed()) window.webContents.send("workbench:event", { type: "session_loaded", initial: value });
    return value;
  } catch (error) {
    sessions.delete(id);
    await transport.close();
    throw error;
  }
}

async function mutateSessions(spec) {
  return globalConfig().request("storage.mutate", spec);
}
async function updateRow(row, patch) {
  return mutateSessions(catalog.updateSpec(row, patch));
}
function running(source) {
  return source.busy || !!source.attachmentImport || [...source.workers.values()].some(status => ["running", "queued"].includes(status)) || [...(source.commands?.values() || [])].includes("running");
}
async function disposeSession(id) {
  const source = sessions.get(id);
  if (!source) return;
  browserCaptures.cancel(id);
  if (source.attachmentImport) await source.attachmentImport.catch(() => {});
  await source.transport.close();
  htmlPreviews.revokeSession(id);
  sessions.delete(id);
  if (window && !window.isDestroyed()) window.webContents.send("workbench:event", { type: "session_unloaded", session: id });
}
async function selectRow(row) {
  if (row.archived || row.scope !== "standalone" && catalog.snapshot().projects.some((project) => projectKey(project.path) === projectKey(row.project) && project.archived))
    throw new Error("请先在设置的已归档会话页面恢复此会话");
  if (row.scope !== "standalone") catalog.updateProject(row.project, { open: true });
  const initial = await createSession(row.project, row.sessionId);
  activeSessionId = row.id;
  saveWorkspace();
  return { ...initial, activeId: row.id, catalog: broadcastCatalog() };
}
async function fallback(project, excluded = [], scope = "project") {
  const snapshot = catalog.snapshot();
  const eligible = [...snapshot.standalone, ...snapshot.projects.filter((entry) => entry.open && !entry.archived)
    .flatMap((entry) => entry.sessions)].filter((row) => !row.archived && !row.error && !excluded.includes(row.id));
  const next = eligible.filter((row) => row.scope === scope && projectKey(row.project) === projectKey(project))
    .sort((a, b) => b.updated - a.updated)[0] || eligible.sort((a, b) => b.updated - a.updated)[0];
  if (next) return selectRow(next);
  const projectScope = snapshot.projects.find((entry) => projectKey(entry.path) === projectKey(project) && entry.open && !entry.archived);
  if (projectScope && scope !== "standalone") return selectRow(catalog.create(projectScope.path));
  activeSessionId = undefined;
  saveWorkspace();
  return { activeId: "", catalog: broadcastCatalog() };
}
async function invoke(method, params) {
  if (method === "app.ready") { uiReady = true; deliverLinks(); return true; }
  if (method === "chat.copy") {
    const row = catalog.find(params.id);
    const url = chatLink(row.id);
    clipboard.writeText(url);
    return { url };
  }
  if (method === "chat.protocol" || method === "chat.register") {
    const supported = app.isPackaged && ["win32", "linux"].includes(process.platform);
    if (method === "chat.register" && (!supported || !app.setAsDefaultProtocolClient("xuloom")))
      throw new Error("此安装尚不能注册系统聊天链接，请使用设置中的粘贴打开，或 CLI --chat；Linux 需要桌面安装集成");
    return { supported, registered: app.isDefaultProtocolClient("xuloom") };
  }
  if (method === "chat.open") {
    parseLink(params.url);
    const resolved = await globalConfig().request("chat.resolve", { url: params.url });
    const snapshot = catalog.snapshot();
    if (snapshot.projects.some(project => projectKey(project.path) === projectKey(resolved.project) && project.archived))
      throw new Error("此项目已归档，请先在设置中恢复");
    if (resolved.scope === "standalone") {
      const before = settings();
      if (!(before.standaloneDirectories || []).some(directory => projectKey(directory) === projectKey(resolved.project)))
        saveSettings({ ...before, standaloneDirectories: [...(before.standaloneDirectories || []), resolved.project] }, before);
    } else if (!snapshot.projects.some(project => projectKey(project.path) === projectKey(resolved.project))) catalog.addProject(resolved.project);
    return selectRow(catalog.find(resolved.id));
  }
  if (method === "clipboard.writeText") {
    if (typeof params.text !== "string") throw new Error("复制内容无效");
    clipboard.writeText(params.text);
    return true;
  }
  if (method === "external.open") {
    if (typeof params.url === "string" && params.url.startsWith("xuloom:")) {
      parseLink(params.url);
      receiveLinks([params.url]);
      return true;
    }
    const url = new URL(params.url);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password)
      throw new Error("只支持 HTTP/HTTPS 网页链接");
    await shell.openExternal(url.href);
    return true;
  }
  if (method === "app.initialize") {
    restoration ||= restoreWorkspace();
    return {
      ...settings(),
      ...await restoration,
      catalog: broadcastCatalog(),
      version: app.getVersion(),
      buildId: JSON.parse(fs.readFileSync(path.join(app.getAppPath(), "package.json"), "utf8")).buildId || "",
      config: await globalConfig().request("config.get"),
      workspaceSettings: workspaceSettings(),
    };
  }
  if (method === "workspace.get") return workspaceSettings();
  if (method === "layout.save") {
    const saved = settings();
    const layout = require("./layout.cjs").updateLayout(saved.layout, params);
    return saveSettings({ ...saved, layout }, saved).layout;
  }
  if (method === "workspace.save") {
    const defaultDirectory = ensureWorkspace(params.defaultDirectory);
    const before = settings();
    saveSettings({ ...before, defaultDirectory }, before);
    return { defaultDirectory };
  }
  if (method === "workspace.choose") {
    const result = await dialog.showOpenDialog(window, {
      title: "选择默认工作目录", properties: ["openDirectory", "createDirectory"],
      defaultPath: workspaceSettings().defaultDirectory,
    });
    return result.canceled ? null : result.filePaths[0];
  }
  if (method === "catalog.refresh") return broadcastCatalog();
  if (method === "archive.restore") {
    if ((typeof params.id === "string") === (typeof params.path === "string")) throw new Error("请选择要恢复的会话或项目");
    catalog.restoreArchive(params);
    return { catalog: broadcastCatalog() };
  }
  if (method === "catalog.view") {
    catalog.setView(params);
    return broadcastCatalog();
  }
  if (method === "session.activate") {
    if (sessions.has(params.session)) {
      activeSessionId = params.session;
      saveWorkspace();
    }
    return true;
  }
  if (method === "config.get") return globalConfig().request("config.get");
  if (["remote.profiles", "remote.save", "remote.remove", "remote.check"].includes(method))
    return globalConfig().request(method, params);
  if (method === "config.models") return globalConfig().request(method, params);
  if (method === "config.delete") {
    if ([...sessions.values()].some(running)) throw new Error("请先停止运行中的任务，再删除 API 连接");
    return globalConfig().request(method, params);
  }
  if (method === "config.reasoning" && [...sessions.values()].some(running))
    throw new Error("请先停止运行中的任务，再更改模型推理设置");
  if (["config.save", "config.select", "config.reasoning"].includes(method) && !params.session)
    return globalConfig().request(method, params);
  if (method === "project.open") {
    let project = params.path;
    if (!project) {
      const result = await dialog.showOpenDialog(window, {
        title: "打开项目目录",
        properties: ["openDirectory"],
      });
      if (result.canceled) return null;
      project = result.filePaths[0];
    }
    if (catalog.snapshot().projects.some((entry) => projectKey(entry.path) === projectKey(fs.realpathSync(project)) && entry.archived))
      throw new Error("项目已归档，请在设置的已归档会话页面恢复");
    project = catalog.addProject(project);
    const active = sessions.get(activeSessionId);
    const resumeId = active && active.scope !== "standalone" && projectKey(active.project) === projectKey(project) ? active.resumeId : latestSavedSession(project);
    return selectRow(resumeId ? catalog.find(sessionKey(project, resumeId)) : catalog.create(project));
  }
  if (method === "session.select") return selectRow(catalog.find(params.id));
  if (method === "session.open" || method === "session.new") {
    let project = params.path;
    const scope = method === "session.new" && !project ? "standalone" : method === "session.open" ? sessions.get(params.session)?.scope || "project" : "project";
    if (method === "session.new" && !project) {
      project = ensureWorkspace(workspaceSettings().defaultDirectory);
    }
    const source = method === "session.new" ? { project } : sessions.get(params.session);
    if (!source || scope !== "standalone" && !catalog.snapshot().projects.some((entry) => projectKey(entry.path) === projectKey(source.project))) throw new Error("项目不存在");
    if (scope !== "standalone" && catalog.snapshot().projects.some((entry) => projectKey(entry.path) === projectKey(source.project) && entry.archived))
      throw new Error("请先在设置的已归档会话页面恢复项目");
    if (params.resumeId) return selectRow(catalog.find(sessionKey(source.project, params.resumeId)));
    const empty = scope !== "standalone" && catalog.list(source.project).find((row) => row.scope === scope && !row.archived && !row.error && !row.draft && !row.draftAttachments.length &&
      row.title === "新会话" && !running(sessions.get(row.id) || { busy: false, workers: new Map() }) &&
      JSON.parse(fs.readFileSync(catalog.file(row.project, row.sessionId), "utf8")).messages?.length === 0);
    return selectRow(empty || (scope === "standalone" ? catalog.createStandalone(source.project) : catalog.create(source.project)));
  }
  if (method === "session.update") {
    const ids = [...new Set(params.ids || [params.id])];
    for (const id of ids) await updateRow(catalog.find(id), params.patch || {});
    if (params.patch?.archived && ids.includes(activeSessionId)) {
      const active = catalog.find(activeSessionId);
      return fallback(active.project, ids, active.scope);
    }
    return { catalog: broadcastCatalog() };
  }
  if (method === "session.fork") {
    const row = catalog.find(params.id);
    if (sessions.has(row.id) && running(sessions.get(row.id))) throw new Error("先停止当前任务，再复制会话");
    await disposeSession(row.id);
    const spec = catalog.forkSpec(row);
    if (params.through !== undefined) {
      spec.through = params.through;
      spec.title = row.title + "（分支）";
    }
    await mutateSessions(spec);
    return selectRow(catalog.find(sessionKey(spec.target, spec.new_id)));
  }
  if (method === "session.move") {
    const row = catalog.find(params.id), source = sessions.get(row.id);
    if (source && running(source)) throw new Error("请先停止会话和子代理任务，再移动到其他项目");
    const prepared = prepareMove(catalog, row, params.path);
    if (prepared.sameDirectory && row.scope !== "standalone") return selectRow(row);
    if (typeof params.draft === "string") await updateRow(row, { draft: params.draft });
    if (source) {
      source.moving = true;
      await source.transport.close();
      htmlPreviews.revokeSession(row.id);
      sessions.delete(row.id);
    }
    let moved;
    try {
      await mutateSessions(moveSpec(row, prepared.target));
      moved = catalog.find(sessionKey(prepared.target, row.sessionId));
    }
    catch (error) {
      if (source && fs.existsSync(catalog.file(row.project, row.sessionId))) await createSession(row.project, row.sessionId);
      saveWorkspace(); broadcastCatalog(); throw error;
    }
    if (window && !window.isDestroyed()) window.webContents.send("workbench:event", { type: "session_unloaded", session: row.id });
    catalog.updateProject(moved.project, { open: true, collapsed: false });
    try { return { ...await selectRow(moved), movedFrom: row.id }; }
    catch (error) {
      activeSessionId = undefined;
      saveWorkspace();
      const before = settings();
      saveSettings({ ...before, activeConversation: { project: moved.project, resumeId: moved.sessionId, scope: "project" } }, before);
      broadcastCatalog();
      throw new Error("会话记录已移动到目标项目，但引擎打开失败，请重新打开此会话。" + error.message);
    }
  }
  if (method === "session.delete") {
    if (params.confirm !== true) throw new Error("请确认删除会话");
    const rows = [...new Set(params.ids || [params.id])].map((id) => catalog.find(id));
    if (!params.stop && rows.some((row) => sessions.has(row.id) && running(sessions.get(row.id))))
      throw new Error("会话正在运行，请先停止任务或选择停止并删除");
    const wasActive = rows.find((row) => row.id === activeSessionId);
    for (const row of rows) await disposeSession(row.id);
    await mutateSessions({ action: "delete", sessions: rows.map((row) => ({ project: row.project, session_id: row.sessionId })) });
    if (wasActive) return fallback(wasActive.project, rows.map((row) => row.id), wasActive.scope);
    saveWorkspace();
    return { catalog: broadcastCatalog() };
  }
  if (["project.update", "project.close", "project.remove", "project.delete"].includes(method)) {
    const project = catalog.snapshot().projects.find((entry) => projectKey(entry.path) === projectKey(params.path));
    if (!project) throw new Error("项目不存在");
    if (method === "project.update") {
      catalog.updateProject(project.path, params.patch || {});
      if (params.patch?.archived && sessions.get(activeSessionId)?.scope !== "standalone" && sessions.get(activeSessionId)?.project === project.path) return fallback(project.path);
      return { catalog: broadcastCatalog() };
    }
    if (["project.delete", "project.remove"].includes(method) && params.confirm !== true) throw new Error("请确认移除项目");
    const live = [...sessions.entries()].filter(([, value]) => value.scope !== "standalone" && projectKey(value.project) === projectKey(project.path));
    if (!params.stop && live.some(([, source]) => running(source))) throw new Error("项目有运行中的任务，请先停止任务");
    const wasActive = live.some(([id]) => id === activeSessionId);
    for (const [id] of live) await disposeSession(id);
    if (method === "project.delete") await mutateSessions({ action: "delete", sessions: project.sessions.map((row) => ({ project: row.project, session_id: row.sessionId })) });
    if (method === "project.close") catalog.updateProject(project.path, { open: false });
    else catalog.removeProject(project.path);
    if (wasActive) return fallback(project.path);
    saveWorkspace();
    return { catalog: broadcastCatalog() };
  }
  if (method === "session.close") {
    const source = sessions.get(params.session);
    if (source) {
      await disposeSession(params.session);
      if (activeSessionId === params.session) activeSessionId = sessions.keys().next().value;
      saveWorkspace();
    }
    return true;
  }
  if (method === "preview.close") {
    htmlPreviews.revoke(params.token, params.session);
    return true;
  }
  const source = sessions.get(params.session);
  if (!source) throw new Error("请先打开项目");
  if (source.moving) throw new Error("会话正在移动，请稍候");
  if (method === "attachments.import") {
    if (source.busy || source.attachmentImport) throw new Error("请等待当前任务结束后添加附件");
    const row = catalog.find(params.session);
    source.attachmentImport = (async () => {
      const items = await attachments.import(row, params.files, params.existing || []);
      await updateRow(row, { draftAttachments: items.map((item) => item.id) });
      broadcastCatalog();
      return items;
    })();
    try { return await source.attachmentImport; }
    finally { source.attachmentImport = undefined; }
  }
  if (method === "attachments.open") {
    const item = attachments.get(catalog.find(params.session), params.id);
    shell.showItemInFolder(item.path);
    return true;
  }
  if (method === "attachments.preview") {
    const item = attachments.get(catalog.find(params.session), params.id);
    if (item.kind !== "image") throw new Error("该附件不是图片");
    const image = nativeImage.createFromPath(item.path);
    const size = image.getSize();
    const scale = Math.min(2048 / size.width, 2048 / size.height, 1);
    return { name: item.name, source: image.resize({ width: Math.max(1, Math.round(size.width * scale)), height: Math.max(1, Math.round(size.height * scale)), quality: "good" }).toDataURL() };
  }
  if (method === "preview.open") {
    const file = await source.transport.request("files.read", { path: params.path });
    return htmlPreviews.create(params.session, source.project, file.path);
  }
  if (method === "collaboration.set") {
    if (source.busy) throw new Error("请先停止或等待当前任务，再切换协作档位");
    return source.transport.request(method, { mode: params.mode });
  }
  if (method === "permissions.set") {
    if (!["ask", "full"].includes(params.mode)) throw new Error("请选择请求批准或完全访问");
    const row = catalog.find(params.session), previous = row.permissionMode;
    await updateRow(row, { permissionMode: params.mode });
    try {
      const result = await source.transport.request(method, { mode: params.mode });
      broadcastCatalog();
      return result;
    } catch {
      // A lost reply must never leave a hidden full-access engine running.
      await disposeSession(params.session);
      await updateRow(row, { permissionMode: previous });
      broadcastCatalog();
      throw new Error("切换权限失败，当前会话引擎已停止，请重新打开会话");
    }
  }
  if (method === "project.reveal") {
    await shell.openPath(source.project);
    return true;
  }
  if (method === "capabilities.directory") {
    const directory = await source.transport.request(method, params);
    await shell.openPath(directory);
    return true;
  }
  if (method === "capabilities.import") {
    if (!["skills", "agents"].includes(params.kind))
      throw new Error("能力类型无效");
    const result = await dialog.showOpenDialog(window, {
      title:
        params.kind === "skills"
          ? "导入 Skill 或技能集合目录"
          : "导入子代理定义目录",
      properties: ["openDirectory"],
    });
    if (result.canceled) return null;
    return source.transport.request(method, {
      kind: params.kind,
      path: result.filePaths[0],
    });
  }
  if (!engineMethods.has(method)) throw new Error("不支持的操作");
  const { session: _session, ...payload } = params;
  if (method === "prompt" || method === "prompt.resume") {
    if (source.busy || source.attachmentImport) throw new Error("此会话已有运行中的任务或附件正在添加");
    source.busy = true;
    const row = catalog.find(params.session);
    try {
      if (method === "prompt") attachments.list(row, params.attachments || []);
      const result = await source.transport.request(method, payload);
      if (method === "prompt") await updateRow(row, { draft: "", draftEditId: "", draftAttachments: [], ...(row.title === "新会话" ? { title: String(params.text || "").split("\n")[0].slice(0, 80) || "附件会话" } : {}) });
      broadcastCatalog();
      return result;
    }
    catch (error) { source.busy = false; throw error; }
  }
  return source.transport.request(method, payload);
}
app.whenReady().then(() => {
  if (forwardedLink) return;
  const scheduleTimer = setInterval(tickSchedules, 5000);
  scheduleTimer.unref();
  protocol.handle(PREVIEW_SCHEME, (request) => htmlPreviews.respond(request));
  window = new BrowserWindow({
    show: process.env.XULOOM_TEST_HEADLESS !== "1",
    width: 1440,
    height: 940,
    minWidth: 1050,
    minHeight: 700,
    backgroundColor: "#eaf3f7",
    title: "Xuloom · 序织",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      nodeIntegrationInSubFrames: false,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.on("close", (event) => {
    if (!quitting && (sessions.size || configTransport)) {
      event.preventDefault();
      app.quit();
    }
  });
  window.webContents.on("will-prevent-unload", () => { quitting = false; });
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-frame-navigate", (event) => {
    if (!event.isMainFrame && !event.url.startsWith(PREVIEW_SCHEME + "://")) event.preventDefault();
  });
  window.webContents.session.setPermissionRequestHandler(
    (_webContents, _permission, callback) => callback(false),
  );
  ipcMain.handle("workbench:invoke", async (event, method, params) => {
    if (
      event.sender !== window.webContents ||
      event.senderFrame !== window.webContents.mainFrame
    )
      throw new Error("Invalid IPC sender");
    if (typeof method !== "string" || !params || typeof params !== "object")
      throw new Error("Invalid request");
    if (quitting) throw new Error("正在保存并退出，请稍候");
    if (/^(app\.initialize|chat\.(open|copy)|layout\.save|attachments\.import|execution\.set|collaboration\.set|permissions\.set|config\.(save|select|delete|reasoning)|workspace\.save|archive\.restore|project\.(open|update|close|remove|delete)|session\.(open|new|select|update|fork|move|delete|close|activate)|catalog\.)/.test(method)) {
      const operation = workspaceQueue.then(() => invoke(method, params));
      workspaceQueue = operation.catch(() => {});
      return operation;
    }
    return invoke(method, params);
  });
  ipcMain.on("workbench:drafts", (event, drafts) => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame || !Array.isArray(drafts) || drafts.length > 8) {
      event.returnValue = false;
      return;
    }
    let saved = false;
    try {
      for (const draft of drafts) {
        let row; try { row = catalog.find(draft.id); } catch { continue; }
        catalog.update(row, { draft: String(draft.draft || ""), draftEditId: draft.draftEditId || "", draftAttachments: draft.draftAttachments || [] });
      }
      saved = true;
    } catch {}
    // Electron sends a synchronous reply on the first assignment, not on handler return.
    event.returnValue = saved;
  });
  window.on("focus", () => {
    try { if (restoration) broadcastCatalog(); }
    catch (error) { settingsFailure(error); }
  });
  const url = !app.isPackaged && process.env.XULOOM_DEV_URL;
  if (url && url !== "http://127.0.0.1:5178")
    throw new Error("Invalid development server URL");
  if (url) window.loadURL(url);
  else window.loadFile(path.join(__dirname, "../dist/index.html"));
});
app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (quitting || (!sessions.size && !configTransport)) return;
  event.preventDefault();
  quitting = true;
  const flush = workspaceQueue.then(() => window && !window.isDestroyed()
    ? window.webContents.executeJavaScript("window.dispatchEvent(new Event('xuloom-before-quit', { cancelable: true }))").catch(() => false) : true);
  flush.then(async (saved) => {
    if (!saved) { quitting = false; return; }
    const results = await Promise.allSettled(
      [...sessions.values()]
        .map(({ transport }) => transport.close())
        .concat(configTransport ? [configTransport.close()] : []),
    );
    const failed = results.find((result) => result.status === "rejected");
    if (failed) {
      quitting = false;
      settingsFailure(new Error("关闭引擎失败，窗口和草稿已保留；请检查存储或资源占用后重试退出。" + failed.reason), "");
      return;
    }
    sessions.clear();
    app.quit();
  });
});
