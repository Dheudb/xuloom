const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { historyRoot, migrateProject } = require("./history-storage.cjs");
const { dataDirectory } = require("./workspace.cjs");
const { mutateSync } = require("./storage-helper.cjs");

const validId = (id) => typeof id === "string" && /^[a-zA-Z0-9_-]+$/.test(id);
const projectKey = (project) => process.platform === "win32"
  ? path.resolve(project).toLowerCase() : path.resolve(project);
const sessionKey = (project, id) => crypto.createHash("sha256")
  .update(projectKey(project)).digest("hex").slice(0, 16) + ":" + id;
const basename = (project) => path.basename(project);

function atomicJSON(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = target + "." + crypto.randomUUID() + ".tmp";
  try {
    fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { encoding: "utf8", flag: "wx" });
    fs.renameSync(temporary, target);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

/** Durable sidebar data. Runtime processes are a cache, not the session list. */
class SessionCatalog {
  constructor(readSettings, writeSettings, dataRoot = dataDirectory({ home: require("node:os").homedir() })) {
    this.readSettings = readSettings;
    this.writeSettings = writeSettings;
    this.dataRoot = dataRoot;
  }
  directory(project) {
    const base = path.resolve(project);
    const root = path.join(migrateProject(base, this.dataRoot, atomicJSON), "sessions");
    if (fs.existsSync(root) && !projectKey(fs.realpathSync(root)).startsWith(projectKey(path.resolve(this.dataRoot)) + path.sep))
      throw new Error("会话目录必须位于集中存储中");
    return root;
  }
  file(project, id) {
    if (!validId(id)) throw new Error("会话 ID 无效");
    const root = this.directory(project);
    const target = path.join(root, id + ".json");
    if (fs.existsSync(target) && (fs.lstatSync(target).isSymbolicLink() ||
      path.dirname(fs.realpathSync(target)) !== fs.realpathSync(root)))
      throw new Error("会话文件必须位于项目的历史目录中");
    return target;
  }
  indexPath(project) { return path.join(this.directory(project), "..", "index.json"); }
  artifacts(project) { return path.join(historyRoot(project, this.dataRoot), "tool-results"); }
  index(project) {
    const target = this.indexPath(project);
    if (!fs.existsSync(target)) return Object.create(null);
    const data = JSON.parse(fs.readFileSync(target, "utf8"));
    if (!data || data.version !== 1 || typeof data.sessions !== "object" || !data.sessions)
      throw new Error("会话索引损坏，请先修复 session-index.json");
    return Object.assign(Object.create(null), data.sessions);
  }
  list(project) {
    const root = this.directory(project);
    if (!fs.existsSync(root)) return [];
    const meta = this.index(project);
    return fs.readdirSync(root).filter((name) => name.endsWith(".json") && validId(name.slice(0, -5)))
      .flatMap((name) => {
        const sessionId = name.slice(0, -5);
        try {
          const target = this.file(project, sessionId);
          const stat = fs.statSync(target);
          const raw = JSON.parse(fs.readFileSync(target, "utf8"));
          const messages = Array.isArray(raw) ? raw : raw.messages || [];
          const first = messages.find((message) => message.role === "user" && !message._is_meta &&
            !["team", "subagent"].includes(message._origin));
          const text = typeof first?._display_text === "string" ? first._display_text : typeof first?.content === "string" ? first.content :
            (first?.content || []).filter((block) => block.type === "text").map((block) => block.text).join("\n");
          const item = meta[sessionId] || {};
          return [{ id: sessionKey(project, sessionId), sessionId, project,
            scope: item.scope === "standalone" ? "standalone" : "project",
            title: item.title || text.split("\n")[0].slice(0, 80) || "新会话",
            updated: stat.mtimeMs, created: item.created || stat.birthtimeMs || stat.mtimeMs,
            pinned: !!item.pinned, archived: !!item.archived, draft: item.draft || "", draftEditId: item.draftEditId || "", draftAttachments: item.draftAttachments || [], permissionMode: item.permissionMode === "full" ? "full" : "ask" }];
        } catch (error) {
          // Keep an unreadable record visible and deletable instead of hiding it.
          return [{ id: sessionKey(project, sessionId), sessionId, project,
            scope: meta[sessionId]?.scope === "standalone" ? "standalone" : "project",
            title: meta[sessionId]?.title || "无法读取的会话", updated: 0, created: 0,
            pinned: !!meta[sessionId]?.pinned, archived: !!meta[sessionId]?.archived, permissionMode: meta[sessionId]?.permissionMode === "full" ? "full" : "ask",
            error: error.message }];
        }
      });
  }
  snapshot() {
    const settings = this.readSettings();
    const projects = [...new Set(settings.projects || [])].map((project) => {
      const meta = settings.projectMeta?.[projectKey(project)] || {};
      try {
        return { path: project, name: meta.name || basename(project), open: meta.open !== false,
          pinned: !!meta.pinned, archived: !!meta.archived, collapsed: !!meta.collapsed,
          sessions: this.list(project).filter((row) => row.scope !== "standalone") };
      } catch (error) {
        return { path: project, name: meta.name || basename(project), open: false,
          pinned: !!meta.pinned, archived: !!meta.archived, sessions: [], error: error.message };
      }
    });
    const roots = [...new Map([...(settings.standaloneDirectories || []), ...(settings.projects || [])]
      .map((directory) => [projectKey(directory), directory])).values()];
    const standalone = roots.flatMap((directory) => {
      try { return this.list(directory).filter((row) => row.scope === "standalone"); }
      catch { return []; }
    });
    const sort = settings.sidebar?.sort;
    return { projects, standalone, view: { sort: ["recent", "oldest", "name", "created"].includes(sort) ? sort : "recent" } };
  }
  find(id) {
    const snapshot = this.snapshot();
    const row = [...snapshot.standalone, ...snapshot.projects.flatMap((project) => project.sessions)].find((row) => row.id === id);
    if (!row) throw new Error("会话不存在，可能已被删除");
    return row;
  }
  addProject(project) {
    project = fs.realpathSync(project);
    if (!fs.statSync(project).isDirectory()) throw new Error("项目目录不存在");
    const before = this.readSettings(), settings = structuredClone(before);
    settings.projects = [project, ...(settings.projects || []).filter((p) => projectKey(p) !== projectKey(project))];
    settings.projectMeta ||= {};
    settings.projectMeta[projectKey(project)] = { ...settings.projectMeta[projectKey(project)], open: true, archived: false };
    this.writeSettings(settings, before);
    return project;
  }
  updateProject(project, patch) {
    const before = this.readSettings(), settings = structuredClone(before);
    if (!(settings.projects || []).some((p) => projectKey(p) === projectKey(project))) throw new Error("项目不存在");
    settings.projectMeta ||= {};
    const next = { ...settings.projectMeta[projectKey(project)] };
    for (const field of ["pinned", "archived", "collapsed", "open"]) {
      if (field in patch) next[field] = !!patch[field];
    }
    if ("name" in patch) {
      const name = String(patch.name).trim();
      if (!name || name.length > 100) throw new Error("项目名称需为 1–100 个字符");
      next.name = name;
    }
    settings.projectMeta[projectKey(project)] = next;
    this.writeSettings(settings, before);
  }
  removeProject(project) {
    const before = this.readSettings(), settings = structuredClone(before);
    settings.projects = (settings.projects || []).filter((p) => projectKey(p) !== projectKey(project));
    if (settings.projectMeta) delete settings.projectMeta[projectKey(project)];
    this.writeSettings(settings, before);
  }
  createStandalone(directory) {
    const base = fs.realpathSync(directory);
    const identity = crypto.randomUUID();
    directory = path.join(base, "chat-" + identity);
    const before = this.readSettings(), settings = structuredClone(before);
    settings.standaloneDirectories = [directory, ...(settings.standaloneDirectories || []).filter((p) => projectKey(p) !== projectKey(directory))];
    this.writeSettings(settings, before);
    mutateSync(this, { action: "create", project: directory, session_id: identity, scope: "standalone", workspace_base: base, created: Date.now() });
    return this.list(directory).find((row) => row.sessionId === identity);
  }
  async upgradeStandaloneWorkspaces(base, mutate = (spec) => mutateSync(this, spec)) {
    await mutate({ action: "recover" });
    this.reconcileWorkspaceAssociations();
    const roots = [...new Set([...(this.readSettings().standaloneDirectories || []), ...(this.readSettings().projects || [])])];
    const rows = roots.flatMap((directory) => {
      try { return this.list(directory).filter((row) => row.scope === "standalone" && !row.error && !this.index(directory)[row.sessionId]?.workspaceIsolated); }
      catch { return []; } // snapshot() keeps damaged records and migration conflicts visible.
    });
    if (!rows.length) return;
    fs.mkdirSync(base, { recursive: true });
    const { moveSpec } = require("./session-move.cjs");
    for (const row of rows) {
      const identity = "legacy-" + crypto.createHash("sha256").update(row.id).digest("hex").slice(0, 32);
      const target = path.join(base, "chat-" + identity);
      const pendingBefore = this.readSettings(), pendingSettings = structuredClone(pendingBefore);
      pendingSettings.standaloneDirectories = [...new Set([...(pendingSettings.standaloneDirectories || []), target])];
      this.writeSettings(pendingSettings, pendingBefore);
      await mutate({ ...moveSpec(row, target, "standalone"), legacy_workspace: { base, id: identity } });
      const moved = this.find(sessionKey(target, row.sessionId)), before = this.readSettings(), settings = structuredClone(before);
      settings.standaloneDirectories = [...new Set([...(settings.standaloneDirectories || []), moved.project])];
      const matches = (entry) => entry && typeof entry.project === "string" && projectKey(entry.project) === projectKey(row.project) && entry.resumeId === row.sessionId;
      if (matches(settings.activeConversation)) settings.activeConversation = { ...settings.activeConversation, project: moved.project, scope: "standalone" };
      if (settings.workspace) settings.workspace = settings.workspace.map((entry) => matches(entry) ? { ...entry, project: moved.project, scope: "standalone" } : entry);
      this.writeSettings(settings, before);
    }
  }
  reconcileWorkspaceAssociations() {
    const snapshot = this.snapshot(), before = this.readSettings(), settings = structuredClone(before);
    const rows = [...snapshot.standalone, ...snapshot.projects.flatMap((project) => project.sessions)];
    let changed = false;
    const reconcile = (entry) => {
      if (!validId(entry?.resumeId) || typeof entry.project !== "string" || fs.existsSync(this.file(entry.project, entry.resumeId))) return entry;
      for (const row of rows.filter((row) => row.sessionId === entry.resumeId && !row.error)) {
        const raw = JSON.parse(fs.readFileSync(this.file(row.project, row.sessionId), "utf8"));
        const moves = raw.metadata?.workspace_moves || [];
        if (Array.isArray(moves) && typeof moves.at(-1)?.to === "string" && projectKey(moves.at(-1).to) === projectKey(row.project) &&
            moves.some((move) => typeof move?.from === "string" && projectKey(move.from) === projectKey(entry.project))) {
          changed = true;
          return { ...entry, project: row.project, scope: row.scope };
        }
      }
      return entry;
    };
    if (settings.activeConversation) settings.activeConversation = reconcile(settings.activeConversation);
    if (Array.isArray(settings.workspace)) settings.workspace = settings.workspace.map(reconcile);
    if (changed) this.writeSettings(settings, before);
  }
  create(project, messages = [], title, scope = "project") {
    const id = crypto.randomUUID();
    this.directory(project);
    mutateSync(this, { action: "create", project, session_id: id, messages, created: Date.now(), title, scope });
    return this.list(project).find((row) => row.sessionId === id);
  }
  update(row, patch) {
    return mutateSync(this, this.updateSpec(row, patch));
  }
  updateSpec(row, patch) {
    return { action: "metadata", project: row.project, session_id: row.sessionId, patch };
  }
  fork(row) {
    const spec = this.forkSpec(row);
    mutateSync(this, spec);
    return this.find(sessionKey(spec.target, spec.new_id));
  }
  forkSpec(row) {
    const identity = crypto.randomUUID(), target = row.scope === "standalone" ? path.join(path.dirname(row.project), "chat-" + identity) : row.project;
    if (row.scope === "standalone") {
      const before = this.readSettings(), settings = structuredClone(before);
      settings.standaloneDirectories = [...new Set([...(settings.standaloneDirectories || []), target])];
      this.writeSettings(settings, before);
    }
    return { action: "fork", project: row.project, session_id: row.sessionId, target, new_id: identity, scope: row.scope, title: row.title + "（副本）" };
  }
  delete(row) {
    mutateSync(this, { action: "delete", sessions: [{ project: row.project, session_id: row.sessionId }] });
  }
  setView(patch) {
    const settings = this.readSettings();
    const view = { sort: this.snapshot().view.sort };
    if (patch.sort && ["recent", "oldest", "name", "created"].includes(patch.sort)) view.sort = patch.sort;
    this.writeSettings({ ...settings, sidebar: view }, settings);
  }
  restoreArchive({ id, path: project }) {
    const row = id ? this.find(id) : null;
    if (row?.scope === "standalone") {
      if (!fs.statSync(row.project).isDirectory()) throw new Error("默认工作目录不存在或无法访问");
      this.update(row, { archived: false });
      return;
    }
    const scope = this.snapshot().projects.find((entry) => projectKey(entry.path) === projectKey(row ? row.project : project));
    if (!scope) throw new Error("归档项目不存在");
    try {
      if (!fs.statSync(scope.path).isDirectory()) throw new Error("not a directory");
    } catch { throw new Error("项目目录不存在或无法访问，请恢复目录后再试"); }
    if (row) this.update(row, { archived: false });
    this.updateProject(scope.path, { archived: false, open: true, collapsed: false });
  }
}
module.exports = { SessionCatalog, projectKey, sessionKey, atomicJSON };
