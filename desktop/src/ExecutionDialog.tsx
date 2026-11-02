import { useEffect, useState } from "react";
import { X } from "lucide-react";

export default function ExecutionDialog({ invoke, close, saved, onError }: {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  close: () => void; saved: (mode: "host" | "workspace") => void; onError: (e: unknown) => void;
}) {
  const [status, setStatus] = useState<any>(), [pending, setPending] = useState(false);
  useEffect(() => { invoke("execution.get").then(setStatus).catch(onError); }, []);
  async function select(mode: "host" | "workspace") {
    setPending(true);
    try { const value = await invoke("execution.set", { mode }); setStatus(value); saved(mode); close(); }
    catch (error) { onError(error); } finally { setPending(false); }
  }
  return <div className="overlay" onMouseDown={(event) => { if (!pending && event.target === event.currentTarget) close(); }}>
    <section className="dialog" role="dialog" aria-modal="true" aria-label="执行隔离设置" onKeyDown={(e) => { if (e.key === "Escape" && !pending) close(); }}>
      <header><h2>执行隔离</h2><button aria-label="关闭执行隔离设置" disabled={pending} onClick={close}><X size={17} /></button></header>
      <p>与工具审批独立。完全访问也不会绕过系统隔离。</p>
      <button className="import-button" disabled={pending || !status} onClick={() => void select("host")}>宿主执行 {status?.mode === "host" ? "· 当前" : ""}</button>
      <p>工具使用当前用户的系统权限；适合可信任务。</p>
      <button className="import-button" disabled={pending || !status?.available} onClick={() => void select("workspace")}>工作区系统隔离 {status?.mode === "workspace" ? "· 当前" : ""}</button>
      <p>Windows LPAC：工作目录可读写，运行时只读，禁止网络，限制子进程并在结束时清理。</p>
      <p>仅文件工具、命令与 REPL 已接入。MCP、浏览器、LSP 和旧版 Shell 工具会禁用；Linux/macOS 后端尚未提供。</p>
      {status && !status.available && <p>当前平台尚无可用的系统隔离后端。</p>}
      {status?.cleanup_issues?.map((issue: any) => <div className="error" role="alert" key={issue.id}>
        <p>{issue.status === "record_invalid" ? "清理记录损坏，无法确认残留状态。原文件已保留，请检查记录后再重试。" : "上次隔离执行的权限清理尚未完成。执行结果保留，未自动重跑任务。"}</p>
        <p className="extension-path">记录：{issue.report_path}</p>
        {issue.error && <p>{issue.error}</p>}
        <button disabled={pending || issue.status === "record_invalid"} onClick={async () => {
          setPending(true);
          try { setStatus(await invoke("execution.cleanup", { id: issue.id })); }
          catch (error) { onError(error); } finally { setPending(false); }
        }}>重试清理权限</button>
      </div>)}
    </section>
  </div>;
}
