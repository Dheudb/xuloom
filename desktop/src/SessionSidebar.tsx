import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, ChevronDown, ChevronRight, Copy, Folder, FolderOpen,
  LoaderCircle, MessageSquare, MoreHorizontal, Pencil, Pin, Plus, RefreshCw, Search,
  Trash2, X, CheckSquare, LogOut, FolderInput, Link2, Server } from "lucide-react";
import type { CatalogProject, CatalogSession, SessionCatalog, SessionView } from "./types";

type Menu = { kind: "session"; row: CatalogSession } | { kind: "project"; row: CatalogProject };
export default function SessionSidebar({ catalog, activeId, live, opening, action, openProject, onError }: {
  catalog: SessionCatalog;
  activeId: string;
  live: SessionView[];
  opening: boolean;
  action: (method: string, params: Record<string, unknown>) => Promise<void>;
  openProject: (path?: string) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState<Menu | null>(null);
  const [armed, setArmed] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [rename, setRename] = useState<Menu | null>(null);
  const [name, setName] = useState("");
  const [remove, setRemove] = useState<{ row: CatalogProject; deleteHistory: boolean } | null>(null);
  const [move, setMove] = useState<CatalogSession | null>(null);
  const [destination, setDestination] = useState("");
  const [dragging, setDragging] = useState("");
  const [dropTarget, setDropTarget] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (menu) requestAnimationFrame(() => menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus());
  }, [menu]);
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(""), 6000);
    return () => clearTimeout(timer);
  }, [armed]);
  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) { setMenu(null); setArmed(""); }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !working) { setMenu(null); setArmed(""); setRename(null); setRemove(null); setMove(null); setDragging(""); setDropTarget(""); }
    };
    document.addEventListener("click", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("click", close); document.removeEventListener("keydown", escape); };
  }, [working]);
  useEffect(() => {
    const ids = new Set(catalog.projects.filter((entry) => !entry.archived)
      .flatMap((entry) => entry.sessions.filter((row) => !row.archived).map((row) => row.id))
      .concat((catalog.standalone || []).filter((row) => !row.archived).map((row) => row.id)));
    setSelected((previous) => previous.filter((id) => ids.has(id)));
  }, [catalog]);
  const busy = (id: string) => live.some((session) => session.id === id &&
    (session.busy || session.workers.some((worker) => worker.status === "running")));
  const run = async (method: string, params: Record<string, unknown>) => {
    setMenu(null); setArmed("");
    try { await action(method, params); if (Array.isArray(params.ids)) setSelected([]); } catch (e) { onError(e); }
  };
  const toggleSelected = (id: string) => setSelected((previous) => previous.includes(id)
    ? previous.filter((value) => value !== id) : [...previous, id]);
  const requestDelete = (rows: CatalogSession[], batch = false) => {
    const token = batch ? "batch" : rows[0].id;
    if (armed !== token) { setArmed(token); return; }
    void run("session.delete", { ids: rows.map((row) => row.id), confirm: true, stop: rows.some((row) => busy(row.id)) });
  };
  const sortRows = (rows: CatalogSession[]) => [...rows].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    if (catalog.view.sort === "name") return a.title.localeCompare(b.title, "zh-CN");
    if (catalog.view.sort === "oldest") return a.updated - b.updated;
    if (catalog.view.sort === "created") return b.created - a.created;
    return b.updated - a.updated;
  });
  const needle = query.trim().toLocaleLowerCase();
  const allRows = [...(catalog.standalone || []).filter((row) => !row.archived), ...catalog.projects.filter((entry) => !entry.archived).flatMap((entry) => entry.sessions.filter((row) => !row.archived))];
  const standalone = sortRows((catalog.standalone || []).filter((row) => !row.archived && (!needle || row.title.toLocaleLowerCase().includes(needle))));
  const selectedRows = allRows.filter((row) => selected.includes(row.id));
  const projects = catalog.projects.filter((entry) => !entry.archived)
    .sort((a, b) => Number(b.pinned) - Number(a.pinned));
  async function submitDialog() {
    setWorking(true); setError("");
    try {
      if (rename) await action(rename.kind === "session" ? "session.update" : "project.update",
        rename.kind === "session" ? { id: rename.row.id, patch: { title: name } } : { path: rename.row.path, patch: { name } });
      if (remove) await action(remove.deleteHistory ? "project.delete" : "project.remove", {
        path: remove.row.path, confirm: true, stop: true,
      });
      if (move) await action("session.move", { id: move.id, path: destination, draft: live.find((session) => session.id === move.id)?.draft ?? move.draft ?? "" });
      setRename(null); setRemove(null); setMove(null);
    } catch (e) { setError(String(e)); } finally { setWorking(false); }
  }
  function openMenu(event: React.MouseEvent, value: Menu) {
    event.preventDefault(); event.stopPropagation();
    setMenu(value); setArmed("");
    const rect = event.currentTarget.getBoundingClientRect();
    setMenuPosition({ top: Math.max(8, Math.min(event.type === "contextmenu" ? event.clientY : rect.bottom, window.innerHeight - 340)),
      left: Math.max(8, Math.min(rect.right, window.innerWidth - 205)) });
  }
  function renderMenu(value: Menu) {
    if (!menu || menu.kind !== value.kind || (menu.kind === "session"
      ? menu.row.id !== (value.row as CatalogSession).id : menu.row.path !== (value.row as CatalogProject).path)) return null;
    const row = value.row;
    return createPortal(<div role="menu" aria-label={value.kind === "session" ? "会话操作" : "项目操作"}
      className="sidebar-row-menu" style={{ position: "fixed", ...menuPosition }} ref={menuRef} onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const buttons = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") || []);
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
          (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next]?.focus();
      }}>
      {value.kind === "session" ? <>
        <button role="menuitem" onClick={() => run("chat.copy", { id: value.row.id })}><Link2 size={14} />复制聊天链接</button>
        <button role="menuitem" onClick={() => { setRename(value); setName(value.row.title); setMenu(null); setError(""); }}><Pencil size={14} />重命名会话</button>
        <button role="menuitem" onClick={() => run("session.update", { id: value.row.id, patch: { pinned: !value.row.pinned } })}><Pin size={14} />{value.row.pinned ? "取消置顶" : "置顶会话"}</button>
        <button role="menuitem" onClick={() => run("session.update", { id: value.row.id, patch: { archived: true } })}>
          <Archive size={14} />归档会话</button>
        <button role="menuitem" disabled={busy(value.row.id)} onClick={() => run("session.fork", { id: value.row.id })}><Copy size={14} />复制为新会话</button>
        <button role="menuitem" disabled={!!value.row.remote || busy(value.row.id) || !projects.some((project) => !project.remote && (value.row.scope === "standalone" || project.path !== value.row.project))}
          onClick={() => { setMove(value.row); setDestination(projects.find((project) => !project.remote && (value.row.scope === "standalone" || project.path !== value.row.project))?.path || ""); setMenu(null); setError(""); }}><FolderInput size={14} />移动到项目…</button>
        <button role="menuitem" className="danger" onClick={() => requestDelete([value.row])}><Trash2 size={14} />
          {armed === value.row.id ? (busy(value.row.id) ? "确认停止并删除" : "确认删除会话") : (busy(value.row.id) ? "停止并删除会话" : "删除会话")}</button>
      </> : <>
        <button role="menuitem" onClick={() => run("session.new", { path: value.row.path })}><Plus size={14} />新建会话</button>
        <button role="menuitem" onClick={() => { setRename(value); setName(value.row.name); setMenu(null); setError(""); }}><Pencil size={14} />重命名项目</button>
        <button role="menuitem" onClick={() => run("project.update", { path: value.row.path, patch: { pinned: !value.row.pinned } })}><Pin size={14} />{value.row.pinned ? "取消置顶" : "置顶项目"}</button>
        <button role="menuitem" onClick={() => run("project.update", { path: value.row.path, patch: { archived: true } })}><Archive size={14} />归档项目</button>
        {value.row.open && <button role="menuitem" onClick={() => run("project.close", { path: value.row.path })}><LogOut size={14} />关闭项目</button>}
        <button role="menuitem" disabled={!!value.row.remote} onClick={() => { setRemove({ row: value.row, deleteHistory: false }); setMenu(null); setError(""); }}><X size={14} />从列表移除项目</button>
        <button role="menuitem" className="danger" disabled={!!value.row.remote} onClick={() => { setRemove({ row: value.row, deleteHistory: true }); setMenu(null); setError(""); }}><Trash2 size={14} />删除项目会话记录</button>
      </>}
    </div>, document.body);
  }
  function renderSession(row: CatalogSession) {
    return <div className={"session-item " + (row.id === activeId ? "active " : "") + (row.archived ? "archived " : "") + (dragging === row.id ? "dragging" : "")}
      data-session-id={row.id} key={row.id} draggable={!row.remote && !opening && !busy(row.id)}
      title={row.remote ? "SSH · " + row.remote.host : busy(row.id) ? "先停止会话和子代理任务，再拖动到其他项目" : "拖动到项目以切换工作目录"}
      onDragStart={(event) => { if (opening || busy(row.id)) { event.preventDefault(); return; }
        event.dataTransfer.setData("application/x-xuloom-session", row.id); event.dataTransfer.effectAllowed = "move";
        setDragging(row.id); setMenu(null); }}
      onDragEnd={() => { setDragging(""); setDropTarget(""); }}
      onContextMenu={(e) => openMenu(e, { kind: "session", row })}>
      {choosing && <input type="checkbox" aria-label={"选择会话 " + row.title} disabled={opening} checked={selected.includes(row.id)} onChange={() => toggleSelected(row.id)} />}
      <button className="session-select" title={row.error || row.title} disabled={opening} onClick={() => choosing ? toggleSelected(row.id) : run("session.select", { id: row.id })}>
        {busy(row.id) ? <LoaderCircle size={14} className="spin" /> : <MessageSquare size={14} />}
        <span><strong>{row.title}</strong><small className={row.error || row.draft || row.draftAttachments?.length ? "session-note" : "session-date"}>{row.error ? "读取失败" : row.draft || row.draftAttachments?.length ? "有草稿" : row.updated ? new Date(row.updated).toLocaleDateString("zh-CN") : ""}</small></span>
        {row.pinned && <Pin size={11} />}{row.archived && <Archive size={11} />}
      </button>
      <button className="icon-button row-menu-trigger" aria-label={"会话操作 " + row.title} onClick={(e) => openMenu(e, { kind: "session", row })}><MoreHorizontal size={15} /></button>
      {renderMenu({ kind: "session", row })}
    </div>;
  }
  return <>
    <div className="sidebar-catalog-toolbar">
      <label className="session-search"><Search size={14} /><input aria-label="搜索项目或会话" placeholder="搜索项目或会话" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      <div className="catalog-view-controls">
        <select aria-label="会话排序" value={catalog.view.sort} onChange={(e) => run("catalog.view", { sort: e.target.value })}>
          <option value="recent">最近更新</option><option value="oldest">最早更新</option><option value="name">名称排序</option><option value="created">最近创建</option>
        </select>
        <button className={"icon-button " + (choosing ? "selected" : "")} aria-label="选择多个会话" title="批量管理" onClick={() => { setChoosing(!choosing); setSelected([]); }}><CheckSquare size={15} /></button>
        <button className="icon-button" aria-label="刷新会话列表" onClick={() => run("catalog.refresh", {})}><RefreshCw size={14} /></button>
      </div>
      {choosing && selectedRows.length > 0 && <div className="batch-actions" onClick={(event) => event.stopPropagation()}>
        <span>已选 {selectedRows.length} 项</span>
        <button aria-label="归档选中会话" disabled={opening} onClick={() => run("session.update", { ids: selected, patch: { archived: true } })}><Archive size={14} /></button>
        <button aria-label={armed === "batch" ? "确认删除选中会话" : "删除选中会话"} disabled={opening} className="danger" onClick={() => requestDelete(selectedRows, true)}><Trash2 size={14} />{armed === "batch" ? "确认删除" : "删除"}</button>
      </div>}
    </div>
    <div className="sidebar-scroll catalog-scroll">
      {!projects.length && !standalone.length && <p className="sidebar-hint">新建独立会话，或创建项目开始工作。</p>}
      {!!standalone.length && <section className="standalone-group" aria-label="独立会话">
        <div className="section-label">独立会话</div><div className="standalone-sessions">{standalone.map(renderSession)}</div>
      </section>}
      {projects.map((project) => {
        const projectMatches = project.name.toLocaleLowerCase().includes(needle);
        const rows = sortRows(project.sessions.filter((row) => !row.archived &&
          (!needle || projectMatches || row.title.toLocaleLowerCase().includes(needle))));
        if (needle && !projectMatches && !rows.length) return null;
        const dragged = allRows.find((row) => row.id === dragging);
        const acceptsDrop = !!dragged && !dragged.remote && !project.remote && !opening && (dragged.scope === "standalone" || dragged.project !== project.path);
        return <section className={"project-group " + (!project.open ? "closed " : "") + (dropTarget === project.path ? "drop-target" : "")} key={project.path} data-project={project.path}
          onDragOver={(event) => { if (acceptsDrop) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; setDropTarget(project.path); } }}
          onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTarget(""); }}
          onDrop={(event) => { event.preventDefault(); event.stopPropagation(); setDropTarget(""); setDragging("");
            if (!acceptsDrop || event.dataTransfer.getData("application/x-xuloom-session") !== dragging) return;
            void run("session.move", { id: dragging, path: project.path, draft: live.find((session) => session.id === dragging)?.draft ?? dragged?.draft ?? "" }); }}>
          <div className="project-heading" onContextMenu={(e) => openMenu(e, { kind: "project", row: project })}>
            <button className="icon-button project-collapse" aria-label={(project.collapsed ? "展开项目 " : "折叠项目 ") + project.name}
              onClick={() => run("project.update", { path: project.path, patch: { collapsed: !project.collapsed } })}>
              {project.collapsed || !project.open ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</button>
            <button className="project-item" aria-label={"打开项目 " + project.name} title={project.path} disabled={opening} onClick={() => openProject(project.path)}>
              {project.remote ? <Server size={14} /> : project.open ? <FolderOpen size={14} /> : <Folder size={14} />}<span>{project.name}</span>
              {project.pinned && <Pin size={11} />}{project.archived && <Archive size={11} />}<small>{project.open ? rows.length : "已关闭"}</small>
            </button>
            <button className="icon-button project-new" aria-label={"在项目 " + project.name + " 中新建会话"} title="在此项目中新建会话" disabled={opening} onClick={() => run("session.new", { path: project.path })}><Plus size={14} /></button>
            <button className="icon-button row-menu-trigger" aria-label={"项目操作 " + project.name} onClick={(e) => openMenu(e, { kind: "project", row: project })}><MoreHorizontal size={15} /></button>
            {renderMenu({ kind: "project", row: project })}
          </div>
          {dropTarget === project.path && <div className="project-drop-hint">移到此项目，工作目录将随之切换</div>}
          {project.error && <p className="sidebar-hint error">{project.error}</p>}
          {((project.open && !project.collapsed) || needle) && <div className="project-sessions">
            {!rows.length && <p className="sidebar-hint">{project.sessions.some((row) => row.archived) ? "归档会话可在设置中恢复。" : "还没有会话"}</p>}
            {rows.map(renderSession)}
          </div>}
        </section>;
      })}
    </div>
    {(rename || remove || move) && <div className="overlay" onClick={() => { if (!working) { setRename(null); setRemove(null); setMove(null); } }}>
      <section className="dialog" role="dialog" aria-modal="true" aria-label={move ? "移动会话" : rename ? "重命名" : "移除项目"} onClick={(e) => e.stopPropagation()}>
        <header><h2>{move ? "移动会话" : rename ? "重命名" : remove?.deleteHistory ? "删除项目会话记录" : "从列表移除项目"}</h2></header>
        {move && <><p>{move.title}</p><label>目标项目<select aria-label="目标项目" value={destination} onChange={(event) => setDestination(event.target.value)}>
          {projects.filter((project) => !project.remote && (move.scope === "standalone" || project.path !== move.project)).map((project) => <option key={project.path} value={project.path}>{project.name}</option>)}
        </select></label><p className="fine-print">保留历史和草稿，后续任务使用目标项目的目录、Skill、子代理和 MCP 配置。</p></>}
        {rename && <label>名称<input autoFocus aria-label="新名称" value={name} maxLength={rename.kind === "session" ? 200 : 100} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && name.trim() && !working) void submitDialog(); }} /></label>}
        {remove && <><p>{remove.row.name}</p><p>{remove.deleteHistory ? `将永久删除此项目的 ${remove.row.sessions.length} 个会话和对话记录。` : "项目会从侧边栏移除，已有会话记录会保留，重新打开目录即可找回。"}</p>
          <p className="fine-print">项目代码和文件夹会保留。{remove.row.sessions.some((row) => busy(row.id)) ? "此项目正在运行的任务会先停止。" : ""}</p></>}
        {error && <p role="alert" className="error">{error}</p>}
        <footer><button disabled={working} onClick={() => { setRename(null); setRemove(null); setMove(null); }}>取消</button>
          <button className={remove?.deleteHistory ? "primary danger" : "primary"} disabled={working || (!!rename && !name.trim()) || (!!move && !destination)} onClick={() => void submitDialog()}>
            {working ? "处理中…" : move ? "移动会话" : rename ? "保存名称" : remove?.deleteHistory ? "确认删除项目会话" : "确认移除项目"}</button></footer>
      </section>
    </div>}
  </>;
}
