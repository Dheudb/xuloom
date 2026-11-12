import { useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, Folder, MessageSquare, RefreshCw, Search } from "lucide-react";
import type { SessionCatalog } from "./types";
import { sessionTitle, t, tServer, tx } from "./i18n";
export default function ArchivePanel({ catalog, action }: {
  catalog: SessionCatalog;
  action: (method: string, params: Record<string, unknown>) => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const search = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [statusName, setStatusName] = useState("");
  async function run(method: string, params: Record<string, unknown>, message = "", name = "") {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setStatus("");
    try { await action(method, params); setStatus(message); setStatusName(name); }
    catch (e) {
      setError(String(e).replace(/^Error: Error invoking remote method '[^']+': Error: /, ""));
      if (method !== "catalog.refresh") await action("catalog.refresh", {}).catch(() => {});
    } finally { lock.current = false; setBusy(false); search.current?.focus({ preventScroll: true }); }
  }
  useEffect(() => { void run("catalog.refresh", {}); }, []);
  const needle = query.trim().toLocaleLowerCase();
  const matches = (...values: string[]) => !needle || values.some((value) => value.toLocaleLowerCase().includes(needle));
  const projects = catalog.projects.filter((project) => project.archived && matches(project.name, project.path));
  const sessions = [...catalog.projects.flatMap((project) => project.sessions.filter((row) => row.archived &&
    matches(sessionTitle(row), project.name, project.path)).map((row) => ({ row, project }))),
    ...(catalog.standalone || []).filter((row) => row.archived && matches(sessionTitle(row), t("独立会话"), row.project))
      .map((row) => ({ row, project: { name: t("独立会话"), path: row.project, archived: false } }))]
    .sort((a, b) => b.row.updated - a.row.updated);
  const count = catalog.projects.reduce((total, project) => total + project.sessions.filter((row) => row.archived).length, 0) + (catalog.standalone || []).filter((row) => row.archived).length;
  const projectCount = catalog.projects.filter((project) => project.archived).length;
  return <div className="archive-manager" aria-busy={busy}>
    <div className="settings-section-heading"><div><h3>{t("已归档会话")}</h3>
      <p>{t("归档不删除历史；SSH 会话仍保存在远端。恢复后会重新显示在侧边栏。")}</p></div>
      <button className="icon-button" aria-label={t("刷新归档列表")} disabled={busy} onClick={() => void run("catalog.refresh", {})}><RefreshCw size={16} /></button>
    </div>
    <label className="archive-search"><Search size={16} /><input ref={search} aria-label={t("搜索归档记录")} placeholder={t("搜索会话、项目或路径")} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
    <p className="archive-count">{tx("{count} 个归档会话 · {projectCount} 个归档项目", { count, projectCount })}</p>
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {status && <p role="status" className="archive-status">{tServer(status)}{statusName && <span>{statusName}</span>}</p>}
    {!!projects.length && <section aria-label={t("已归档项目")}><h4>{t("已归档项目")}</h4>
      {projects.map((project) => <article className="archive-row" key={project.path} data-archived-project={project.path}>
        <Folder size={18} /><div className="archive-row-info"><strong>{project.name}</strong>
          <span title={project.path}>{project.path}</span><small>{tx("{length} 个会话{value}", { length: project.sessions.length, value: project.error ? t(" · 无法读取项目，请检查目录和访问权限") : "" })}</small></div>
        <button disabled={busy} aria-label={t("恢复项目 {name}", { name: project.name })} onClick={() => void run("archive.restore", { path: project.path }, t("已恢复项目："), project.name)}><ArchiveRestore size={15} />{t("恢复项目")}</button>
      </article>)}
    </section>}
    {!!sessions.length && <section aria-label={t("归档会话列表")}><h4>{t("会话")}</h4>
      {sessions.map(({ row, project }) => <article className="archive-row" key={row.id} data-archived-session={row.id}>
        <MessageSquare size={18} /><div className="archive-row-info"><strong>{sessionTitle(row)}</strong>
          <span title={project.path}>{project.name} · {project.path}</span>
          <small>{row.updated ? new Date(row.updated).toLocaleString("zh-CN") : t("时间未知")}{row.error ? t(" · 记录读取失败") : ""}{project.archived ? t(" · 恢复此会话时也将恢复所属项目") : ""}</small></div>
        <button disabled={busy} aria-label={t("恢复会话 {title}", { title: sessionTitle(row) })} onClick={() => void run("archive.restore", { id: row.id }, t("已恢复会话："), sessionTitle(row))}><ArchiveRestore size={15} />{t("恢复")}</button>
      </article>)}
    </section>}
    {!projects.length && !sessions.length && <div className="archive-empty"><Archive size={32} />
      <strong>{busy ? t("正在读取归档记录…") : needle ? t("没有匹配的归档记录") : t("暂无归档记录")}</strong>
      <p>{busy ? "" : needle ? t("试试其他会话名称或项目路径。") : t("在侧边栏的会话或项目菜单中归档后，可以在这里恢复。")}</p>
    </div>}
  </div>;
}
