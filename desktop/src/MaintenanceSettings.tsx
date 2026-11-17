import { useEffect, useState } from "react";
import { t, tServer } from "./i18n";

export default function MaintenanceSettings() {
  const [status, setStatus] = useState<any>({});
  const [result, setResult] = useState<any>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const invoke = (action: string) => window.xuloom.invoke("app.maintenance." + action);
  useEffect(() => { invoke("status").then(setStatus).catch(e => setError(String(e))); }, []);
  async function run(action: string) {
    setBusy(true); setError(""); setConfirm(false);
    try { const value = await invoke(action); setResult(value); setStatus(await invoke("status")); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <section className="maintenance-settings">
    <h3>{t("备份与维护")}</h3>
    <p className="fine-print">{t("仅处理本机数据，不调用模型。请先停止任务。")}</p>
    <h4>{t("数据备份与恢复")}</h4>
    <p>{t("备份会话、附件和应用创建的工作区。外部项目文件、扩展代码与密钥不自动迁移；聊天和文件可能含私人信息，请妥善保管备份。")}</p>
    <div className="maintenance-actions"><button disabled={busy} onClick={() => void run("backup")}>{t("导出备份")}</button>
      <button disabled={busy} onClick={() => void run("selectRestore")}>{t("选择备份恢复")}</button></div>
    <p className="fine-print">{t("恢复在正常退出后执行，替换本机会话和自建工作区，并保留旧数据副本；不会恢复或重放任务。连接配置和价格另存为 restored-settings.json，原有密钥不变。")}</p>
    {status.canImportSettings && <button disabled={busy} onClick={() => void run("importSettings")}>{t("导入恢复的连接和价格")}</button>}
    <h4>{t("程序更新与回退")}</h4>
    <p>{t("当前版本")} <span>{status.version}</span></p>
    <p className="fine-print">{t("从同一 Release 下载完整 Windows ZIP 和 SHA256SUMS.txt，放在同一目录。校验通过后，正常退出再替换原安装目录；保留上一版供回退。校验和不等同于数字签名。")}</p>
    <div className="maintenance-actions"><button disabled={busy} onClick={() => void run("releases")}>{t("查看最新版本")}</button>
      <button disabled={busy || !status.packaged} onClick={() => void run("selectUpdate")}>{t("选择更新包")}</button>
      <button disabled={busy || !status.canRollback} onClick={() => void run("rollback")}>{t("回退上一版本")}</button></div>
    {status.pending && <div className="maintenance-confirm">
      <label><input type="checkbox" checked={confirm} onChange={e => setConfirm(e.target.checked)} />{t("我已确认恢复或更新范围，同意正常退出后执行。")}</label>
      <button className="primary" disabled={busy || !confirm} onClick={() => void run("apply")}>{t("退出并应用")}</button>
    </div>}
    <h4>{t("诊断信息")}</h4>
    <p className="fine-print">{t("仅导出版本、运行环境和会话索引统计；不包含消息、工具参数、密钥或环境变量。")}</p>
    <button disabled={busy} onClick={() => void run("diagnose")}>{t("导出诊断")}</button>
    {busy && <p role="status">{t("处理中…")}</p>}
    {result?.path && <p role="status">{result.path}</p>}
    {result?.version && <p>{t("已验证版本")} <span>{result.version}</span></p>}
    {status.state && <p role="status">{t("上次维护状态")} <span>{status.state}</span></p>}
    {status.message && <p role="alert">{tServer(status.message)}</p>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
  </section>;
}
