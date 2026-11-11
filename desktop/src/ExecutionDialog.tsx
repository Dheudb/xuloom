import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { useDialogFocus } from "./useDialogFocus";
import { t, tServer, tx } from "./i18n";

export default function ExecutionDialog({ invoke, close, saved, onError }: {
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  close: () => void; saved: (mode: "host" | "workspace") => void; onError: (e: unknown) => void;
}) {
  const [status, setStatus] = useState<any>(), [pending, setPending] = useState(false);
  const dialogRef = useDialogFocus(t("[aria-label=\"添加工具与模式\"]"));
  useEffect(() => { invoke("execution.get").then(setStatus).catch(onError); }, []);
  async function select(mode: "host" | "workspace") {
    setPending(true);
    try { const value = await invoke("execution.set", { mode }); setStatus(value); saved(mode); close(); }
    catch (error) { onError(error); } finally { setPending(false); }
  }
  return <div className="overlay" onMouseDown={(event) => { if (!pending && event.target === event.currentTarget) close(); }}>
    <section ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-label={t("执行隔离设置")} onKeyDown={(e) => { if (e.key === "Escape" && !pending) close(); }}>
      <header><h2>{t("执行隔离")}</h2><button aria-label={t("关闭执行隔离设置")} disabled={pending} onClick={close}><X size={17} /></button></header>
      <p>{t("与工具审批独立。完全访问也不会绕过系统隔离。")}</p>
      <button className="import-button" disabled={pending || !status} onClick={() => void select("host")}>{tx("宿主执行 {value}", { value: status?.mode === "host" ? t("· 当前") : "" })}</button>
      <p>{t("工具使用当前用户的系统权限；适合可信任务。")}</p>
      <button className="import-button" disabled={pending || !status?.available} onClick={() => void select("workspace")}>{tx("工作区系统隔离 {value}", { value: status?.mode === "workspace" ? t("· 当前") : "" })}</button>
      <p>{t("Windows LPAC：工作目录可读写，运行时只读，禁止网络，限制子进程并在结束时清理。Linux（bubblewrap）和 macOS（sandbox-exec）为实验后端。")}</p>
      <p>{t("仅文件工具、命令与 REPL 已接入。MCP、浏览器、LSP 和旧版 Shell 工具会禁用。")}</p>
      <p>{t("需要联网查资料或调用外部服务的任务，请先确认上述限制；切换为宿主执行只使用你已有的系统权限。")}</p>
      {status && !status.available && <p>{t("当前平台尚无可用的系统隔离后端。")}</p>}
      {status?.cleanup_issues?.map((issue: any) => <div className="error" role="alert" key={issue.id}>
        <p>{issue.status === "record_invalid" ? t("清理记录损坏，无法确认残留状态。原文件已保留，请检查记录后再重试。") : t("上次隔离执行的权限清理尚未完成。执行结果保留，未自动重跑任务。")}</p>
        <p className="extension-path">{tx("记录：{report_path}", { report_path: issue.report_path })}</p>
        {issue.error && <p>{tServer(issue.error)}</p>}
        <button disabled={pending || issue.status === "record_invalid"} onClick={async () => {
          setPending(true);
          try { setStatus(await invoke("execution.cleanup", { id: issue.id })); }
          catch (error) { onError(error); } finally { setPending(false); }
        }}>{t("重试清理权限")}</button>
      </div>)}
    </section>
  </div>;
}
