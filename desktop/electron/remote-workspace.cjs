"use strict";
const crypto = require("node:crypto");
const path = require("node:path");
const { remoteKey: keyOf, remoteId } = require("./chat-links.cjs");
const object = value => !!value && typeof value === "object" && !Array.isArray(value);
const flags = value => ["open", "collapsed", "pinned", "archived"].every(key => !(key in value) || typeof value[key] === "boolean");
function validRow(id, row) {
  try {
    return remoteId(id) && object(row) && object(row.binding) &&
      ["id", "name", "host", "cwd", "python", "platform", "ssh_config"].every(key => typeof row.binding[key] === "string") &&
      Object.keys(row.binding).every(key => ["id", "name", "host", "cwd", "python", "platform", "ssh_config", "port"].includes(key)) &&
      ["name", "host", "cwd", "python", "ssh_config"].every(key => row.binding[key].length <= 2048 && !/[\x00-\x1f\x7f]/.test(row.binding[key])) &&
      /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(row.binding.id) && /^[a-zA-Z0-9][a-zA-Z0-9_.@:\[\]-]{0,254}$/.test(row.binding.host) &&
      (row.binding.port === null || Number.isInteger(row.binding.port) && row.binding.port >= 1 && row.binding.port <= 65535) &&
      (row.binding.platform === "posix" ? path.posix.isAbsolute(row.binding.cwd) : row.binding.platform === "windows" && path.win32.isAbsolute(row.binding.cwd) && !!path.win32.parse(row.binding.cwd).root.replace(/^[\\/]+$/, "")) &&
      id === `ssh:${keyOf(row.binding)}:${row.sessionId}` && typeof row.project === "string" && !!row.project && flags(row) &&
      ["title", "draft", "draftEditId"].every(key => !(key in row) || typeof row[key] === "string" && row[key].length <= (key === "draft" ? 1000000 : key === "title" ? 200 : 128)) &&
      ["created", "updated"].every(key => !(key in row) || Number.isFinite(row[key]) && Math.abs(row[key]) <= 8640000000000000) &&
      (!row.machine || object(row.machine) && ["hostname", "platform", "cwd"].every(key => typeof row.machine[key] === "string" && row.machine[key])) &&
      (!("draftAttachments" in row) || Array.isArray(row.draftAttachments) && !row.draftAttachments.length) &&
      (!("permissionMode" in row) || ["ask", "full"].includes(row.permissionMode));
  } catch { return false; }
}
const validGroup = (key, value) => /^ssh:[0-9a-f]{64}$/.test(key) && object(value) && flags(value) &&
  (!("name" in value) || typeof value.name === "string" && !!value.name.trim() && value.name.length <= 100);

/** Navigation metadata only. Execution, ownership and history stay in the shared runtime. */
function remoteWorkspaces(context) {
  const { settings, saveSettings, sessions, transport, send, broadcast, dispose, running, active, activate, saveWorkspace } = context;
  function rows() {
    const value = settings().remoteChats;
    return object(value) ? Object.entries(value).filter(([id, row]) => validRow(id, row)).map(([id, row]) => ({ ...row, id })) : [];
  }
  function issues() {
    const value = settings();
    return ["remoteChats", "remoteProjects"].reduce((count, field) => count + (!(field in value) ? 0 : !object(value[field]) ? 1 :
      Object.entries(value[field]).filter(([id, row]) => !(field === "remoteChats" ? validRow(id, row) : validGroup(id, row))).length), 0);
  }
  const find = id => { const row = rows().find(row => row.id === id); if (!row) throw new Error("远端会话不存在"); return row; };
  function writable(path) {
    const value = settings();
    if ("remoteChats" in value && !object(value.remoteChats) || "remoteProjects" in value &&
      (!object(value.remoteProjects) || path in value.remoteProjects && !validGroup(path, value.remoteProjects[path])))
      throw new Error("请先修复远端导航设置；原始数据已保留，未尝试连接远端");
  }
  function writeRow(row) {
    const before = settings();
    if ("remoteChats" in before && !object(before.remoteChats)) throw new Error("请先修复远端会话导航；原始数据已保留");
    if (!validRow(row.id, row)) throw new Error("远端会话引用格式无效；原始数据已保留");
    saveSettings({ ...before, remoteChats: { ...before.remoteChats, [row.id]: row } }, before);
    return row;
  }
  function update(id, patch) {
    const row = find(id), next = { ...row };
    if (Object.keys(patch).some(key => !["title", "pinned", "archived", "draft", "draftEditId", "draftAttachments", "permissionMode", "updated"].includes(key))) throw new Error("不支持的远端导航设置");
    for (const key of ["title", "draft", "draftEditId"]) if (key in patch) {
      if (typeof patch[key] !== "string" || patch[key].length > (key === "draft" ? 1000000 : key === "title" ? 200 : 128)) throw new Error("无效的远端会话字段");
      next[key] = patch[key];
    }
    for (const key of ["pinned", "archived"]) if (key in patch) next[key] = !!patch[key];
    if ("permissionMode" in patch) {
      if (!["ask", "full"].includes(patch.permissionMode)) throw new Error("权限模式无效"); next.permissionMode = patch.permissionMode;
    }
    if ("draftAttachments" in patch) {
      if (!Array.isArray(patch.draftAttachments) || patch.draftAttachments.length) throw new Error("远端附件传输尚未接入，未保存本机路径");
      next.draftAttachments = [];
    }
    if ("updated" in patch) next.updated = patch.updated;
    return writeRow(next);
  }
  function group(path) { const row = rows().find(row => `ssh:${keyOf(row.binding)}` === path); if (!row) throw new Error("远端工作区不存在"); return row; }
  function updateProject(path, patch) {
    group(path);
    const before = settings(), next = { ...before.remoteProjects?.[path] };
    if ("remoteProjects" in before && (!object(before.remoteProjects) || path in before.remoteProjects && !validGroup(path, before.remoteProjects[path]))) throw new Error("请先修复远端工作区显示设置；原始数据已保留");
    for (const field of ["open", "collapsed", "archived", "pinned"]) if (field in patch) next[field] = !!patch[field];
    if ("name" in patch) { if (typeof patch.name !== "string" || !patch.name.trim() || patch.name.trim().length > 100) throw new Error("工作区名称需为 1–100 个字符"); next.name = patch.name.trim(); }
    saveSettings({ ...before, remoteProjects: { ...before.remoteProjects, [path]: next } }, before);
  }
  function projects() {
    const groups = new Map(), preferences = settings().remoteProjects;
    for (const row of rows()) {
      const key = keyOf(row.binding), path = `ssh:${key}`, meta = preferences === undefined || object(preferences) && !(path in preferences) ? {} :
        object(preferences) && validGroup(path, preferences[path]) ? preferences[path] : { open: false, archived: true };
      const remote = { connectionId: row.binding.id, host: row.binding.host, name: row.binding.name, key, hostname: row.machine?.hostname };
      if (!groups.has(path)) groups.set(path, { path, name: meta.name || row.binding.name || row.binding.id, open: meta.open !== false,
        collapsed: !!meta.collapsed, pinned: !!meta.pinned, archived: !!meta.archived, remote, sessions: [] });
      groups.get(path).sessions.push({ id: row.id, project: row.project, sessionId: row.sessionId, title: row.title || "新会话",
        created: row.created || 0, updated: row.updated || 0, pinned: !!row.pinned, archived: !!row.archived, scope: "project", remote });
    }
    return [...groups.values()];
  }
  function add(profile, machine, sessionId, initial, title) {
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) throw new Error("远端返回了无效的会话 ID");
    const id = `ssh:${keyOf(profile)}:${sessionId}`, old = rows().find(row => row.id === id);
    return writeRow({ ...old, id, binding: profile, machine, sessionId, project: initial.project, scope: "project",
      title: old?.title || title || initial.history.find(row => row.role === "user")?.text?.split("\n")[0]?.slice(0, 80) || "新会话",
      created: old?.created || Date.now(), updated: old?.updated || Date.now(), permissionMode: old?.permissionMode || "ask",
      draft: old?.draft || "", draftEditId: old?.draftEditId || "", draftAttachments: [], pinned: !!old?.pinned, archived: !!old?.archived });
  }
  async function openRow(row, { newChat = false } = {}) {
    writable(`ssh:${keyOf(row.binding)}`);
    let id = newChat ? null : row.id;
    if (id && (row.archived || settings().remoteProjects?.[`ssh:${keyOf(row.binding)}`]?.archived)) throw new Error("请先恢复归档的远端会话");
    if (id && sessions.has(id)) {
      const existing = sessions.get(id);
      if (!existing.transport.closed && !existing.transport.closeError && !existing.disconnected) {
        activate(id); saveWorkspace(); return { existingId: id, activeId: id, catalog: broadcast() };
      }
      await dispose(id);
    }
    await context.admit();
    const pipe = transport({ id: row.binding.id, binding: row.binding, machine: row.machine }, event => { if (id) send(id, event); });
    try {
      const connection = await pipe.request("remote.connection");
      const sid = newChat || !row.sessionId ? (await pipe.request("sessions.create")).sessionId : row.sessionId;
      const initial = await pipe.request("initialize", { sessionId: sid, permissionMode: row.permissionMode || "ask" });
      row = add(connection.profile, connection.machine, sid, initial, newChat ? "新会话" : row.title);
      id = row.id;
      sessions.set(id, { transport: pipe, project: initial.project, resumeId: sid, scope: "project", remote: { connectionId: row.binding.id, name: row.binding.name, host: row.binding.host, key: keyOf(row.binding), hostname: connection.machine.hostname }, busy: false, workers: new Map() });
      updateProject(`ssh:${keyOf(row.binding)}`, { open: true, collapsed: false });
      activate(id); saveWorkspace();
      return { ...initial, id, title: row.title, scope: "project", remote: sessions.get(id).remote, savedDraft: row.draft, savedDraftEditId: row.draftEditId, savedAttachments: [], activeId: id, catalog: broadcast() };
    } catch (error) { await pipe.close().catch(() => {}); throw error; }
  }
  async function withPipe(row, work) {
    const pipe = transport({ id: row.binding.id, binding: row.binding, machine: row.machine }, () => {});
    try { await pipe.request("remote.connection"); return await work(pipe); } finally { await pipe.close(); }
  }
  async function handle(method, params) {
    if (method === "remote.references") return { handled: true, value: await context.config().request(method) };
    if (method === "remote.forget") {
      if (params.confirm !== true) throw new Error("请确认移除本机引用；远端历史不会删除");
      const { references } = await context.config().request("remote.references");
      const selected = references.find(row => row.id === params.id && row.version === params.version);
      if (!selected) throw new Error("导航引用已更改，请刷新后重试；未移除新数据");
      const affected = [...sessions].filter(([id, source]) => source.remote && (id === selected.chatId || selected.field === "remoteChats" && selected.kind === "container"));
      if (affected.some(([, source]) => running(source))) throw new Error("该远端会话正在运行，请先停止任务；本机引用仍保留");
      for (const [, source] of affected) source.moving = true;
      let value;
      try {
        for (const [id] of affected) await dispose(id);
        value = await context.config().request(method, params);
      } catch (error) {
        for (const [, source] of affected) source.moving = false;
        throw new Error((affected.length ? "远端连接可能已关闭，本机引用尚未移除；请刷新后重试。" : "") + error.message);
      }
      if (affected.some(([id]) => active() === id)) activate(undefined);
      let catalog, notice;
      try { catalog = broadcast(); }
      catch (error) { notice = "本机引用已移除并备份，但历史列表刷新失败：" + error.message; }
      return { handled: true, value: { ...value, activeId: active() || "", catalog, notice } };
    }
    if (method === "remote.open" || method === "remote.history") {
      const { connections } = await context.config().request("remote.profiles");
      const profile = connections.find(item => item.id === params.id); if (!profile) throw new Error("SSH 连接不存在");
      const row = { binding: profile, sessionId: params.sessionId, permissionMode: "ask" };
      if (method === "remote.history") return { handled: true, value: await withPipe(row, pipe => pipe.request("sessions.list")) };
      const saved = params.sessionId && rows().find(row => row.sessionId === params.sessionId && keyOf(row.binding) === keyOf(profile));
      return { handled: true, value: await openRow(saved || row, { newChat: !params.sessionId }) };
    }
    const ids = params.ids || (params.id ? [params.id] : []);
    const isRow = ids.some(remoteId), isPath = typeof params.path === "string" && /^ssh:[0-9a-f]{64}$/.test(params.path);
    if (isRow && ids.some(id => !remoteId(id))) throw new Error("请分别管理本机和远端的会话");
    if (method === "session.select" && isRow) return { handled: true, value: await openRow(find(params.id)) };
    if (["session.new", "project.open"].includes(method) && isPath) {
      const row = method === "project.open" ? rows().filter(row => `ssh:${keyOf(row.binding)}` === params.path && !row.archived).sort((a, b) => b.updated - a.updated)[0] || group(params.path) : group(params.path);
      return { handled: true, value: await openRow(row, { newChat: method === "session.new" }) };
    }
    if (method === "session.update" && isRow) {
      for (const id of ids) update(id, params.patch || {});
      if (params.patch?.archived && ids.includes(active())) { activate(undefined); saveWorkspace(); }
      return { handled: true, value: { ...(params.patch?.archived ? { activeId: active() || "" } : {}), catalog: broadcast() } };
    }
    if (method === "archive.restore" && (isRow || isPath)) {
      const row = isRow ? find(params.id) : group(params.path);
      if (isRow) update(row.id, { archived: false });
      updateProject(`ssh:${keyOf(row.binding)}`, { archived: false, open: true, collapsed: false });
      return { handled: true, value: { catalog: broadcast() } };
    }
    if (method === "project.update" && isPath) {
      updateProject(params.path, params.patch || {});
      if (params.patch?.archived && rows().some(row => row.id === active() && `ssh:${keyOf(row.binding)}` === params.path)) { activate(undefined); saveWorkspace(); }
      return { handled: true, value: { ...(params.patch?.archived ? { activeId: active() || "" } : {}), catalog: broadcast() } };
    }
    if (method === "project.close" && isPath) {
      const closed = rows().filter(row => `ssh:${keyOf(row.binding)}` === params.path);
      if (closed.some(row => sessions.has(row.id) && running(sessions.get(row.id)))) throw new Error("远端工作区有运行中的任务，请先停止后再关闭");
      for (const row of closed) await dispose(row.id);
      if (closed.some(row => row.id === active())) activate(undefined);
      saveWorkspace(); updateProject(params.path, { open: false }); return { handled: true, value: { activeId: active() || null, catalog: broadcast() } };
    }
    if ((method === "session.move" && (isRow || isPath))) throw new Error("暂不支持在本机与 SSH 主机之间移动会话；没有移动文件或历史");
    if (method === "session.delete" && isRow) {
      if (params.confirm !== true) throw new Error("请确认删除远端会话");
      const selected = ids.map(find);
      if (new Set(selected.map(row => keyOf(row.binding))).size !== 1) throw new Error("请按 SSH 工作区分别删除会话");
      if (!params.stop && selected.some(row => sessions.has(row.id) && running(sessions.get(row.id)))) throw new Error("请先停止远端任务，再删除会话");
      for (const row of selected) await dispose(row.id);
      await withPipe(selected[0], pipe => pipe.request("storage.mutate", { action: "delete", sessions: selected.map(row => ({ project: row.project, session_id: row.sessionId })) }));
      const before = settings(), chats = { ...before.remoteChats }; for (const row of selected) delete chats[row.id];
      saveSettings({ ...before, remoteChats: chats }, before);
      if (ids.includes(active())) { activate(undefined); saveWorkspace(); }
      return { handled: true, value: { activeId: active() || null, catalog: broadcast() } };
    }
    if (method === "session.fork" && isRow) {
      const row = find(params.id);
      if (sessions.has(row.id) && running(sessions.get(row.id))) throw new Error("先停止远端任务，再复制会话");
      await dispose(row.id);
      const sid = crypto.randomUUID();
      await withPipe(row, pipe => pipe.request("storage.mutate", { action: "fork", project: row.project, session_id: row.sessionId, target: row.project, new_id: sid, scope: "project", title: row.title + "（分支）", ...(params.through !== undefined ? { through: params.through } : {}) }));
      return { handled: true, value: await openRow({ ...row, id: undefined, sessionId: sid, title: row.title + "（分支）" }) };
    }
    if (["project.remove", "project.delete"].includes(method) && isPath) throw new Error("请在该 SSH 工作区中逐个删除会话；远端目录不会在本机被删除");
    const source = sessions.get(params.session);
    if (!source?.remote) return { handled: false };
    if (["preview.close", "media-preview.close"].includes(method)) return { handled: false };
    if (source.moving) throw new Error("会话正在移除本机引用，请稍候");
    if (source.disconnected && method !== "session.close") throw new Error("SSH 连接已断开，结果未确认。请重新选择会话查看保存状态；不会自动重发任务。");
    if (["attachments.import", "attachments.open", "attachments.preview", "project.reveal", "capabilities.directory", "capabilities.import"].includes(method))
      throw new Error("远端文件传输/网页预览入口尚未接入；未尝试打开本机同名路径");
    if (["session.close", "session.activate", "preview.open", "preview.close", "media-preview.open", "media-preview.close"].includes(method)) return { handled: false };
    const { session, ...payload } = params;
    if (method === "permissions.set") {
      if (!["ask", "full"].includes(params.mode)) throw new Error("权限模式无效");
      try {
        const value = await source.transport.request(method, payload); update(session, { permissionMode: params.mode }); broadcast(); return { handled: true, value };
      } catch (error) {
        await dispose(session); broadcast(); throw new Error("远端权限更改未确认，连接已关闭；请重新打开会话核对状态。" + error.message);
      }
    }
    if (method === "prompt" || method === "prompt.resume" || method === "media.control") {
      if (source.busy) throw new Error("远端会话已有运行中的任务");
      if (params.attachments?.length) throw new Error("远端附件尚未传输；未提交任务");
      source.busy = true;
      try {
        const value = await source.transport.request(method, payload), row = find(session);
        if (method === "prompt") update(session, { draft: "", draftEditId: "", updated: Date.now(), ...(row.title === "新会话" ? { title: String(params.text || "").split("\n")[0].slice(0, 80) } : {}) });
        broadcast(); return { handled: true, value };
      } catch (error) { source.busy = false; throw error; }
    }
    if (!context.methods.has(method) && !["permissions.set", "config.get", "config.models"].includes(method)) throw new Error("不支持的远端操作");
    return { handled: true, value: await source.transport.request(method, payload) };
  }
  async function tickSchedules() {
    // Only attached, idle sessions are eligible. The remote durable scheduler
    // owns claims and completion; this host never copies jobs or replays prompts.
    for (const [id, source] of sessions) {
      if (!source.remote || source.disconnected || source.moving || running(source)) continue;
      const row = rows().find(row => row.id === id);
      if (!row) continue;
      if (row.archived || settings().remoteProjects?.[`ssh:${keyOf(row.binding)}`]?.archived) continue;
      const due = await source.transport.request("schedule.due");
      for (const job of due) {
        if (job.session_id !== source.resumeId || job.cwd !== source.project || source.moving || running(source)) continue;
        await source.transport.request("schedule.deliver", { id: job.id });
      }
    }
  }
  return { handle, projects, issues, find, update, openRow, remoteId, tickSchedules };
}
module.exports = { remoteWorkspaces, remoteId, keyOf };
