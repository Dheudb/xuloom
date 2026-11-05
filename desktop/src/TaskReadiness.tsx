import { useEffect, useState } from "react";

export default function TaskReadiness({invoke}:{invoke:(method:string,params?:Record<string,unknown>)=>Promise<any>}) {
  const [result,setResult]=useState<any>(),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  async function check(){setBusy(true);setError("");try{setResult(await invoke("task.readiness"));}catch(e){setError(String(e));}finally{setBusy(false);}}
  useEffect(()=>{void check();},[]);
  return <section className="task-readiness" aria-label="任务准备检查"><h3>开始任务前</h3><p>先检查模型配置和文件能力，再开始工作。缺项不会自动安装，也不会发送你的文件。</p>
    <button disabled={busy} onClick={()=>void check()}>{busy?"检查中…":"重新检查"}</button>
    {error && <p role="alert" className="error">{error}</p>}
    {result && <div aria-live="polite"><h4>1. 模型连接</h4>
      <p>{result.model.configured?`已保存模型 ${result.model.model}`:"请先在“模型连接”填写 API 地址、Key 并选择模型。"}。本地检查不代表供应商已经接受请求；保存后可在模型连接页获取模型列表，再发送一个不含私密资料的简短任务。</p>
      <h4>2. 文件与产物</h4>{result.formats.map((row:any)=><div key={row.id} className="readiness-row"><strong>{row.label}：{row.available?"解析库已安装":"缺少解析库"}</strong><p>{row.note}</p>{row.install_argv.length>0 && <p>在你选定的 Python 环境中安装 <code>{row.install_argv.at(-1)}</code> 后重新检查；当前不会自动安装或提权。</p>}</div>)}
      <p>办公预览：{result.office_preview?"发现 LibreOffice 命令，实际渲染仍需验证":"未发现 LibreOffice 命令，不能宣称版式已核验"}。</p>
      <h4>3. 工具与验收</h4><p>{result.network_tools_available?"宿主执行允许按现有审批规则使用联网工具。":"当前工作区隔离禁用联网与尚未接入隔离的工具，联网研究需要先调整任务或执行模式。"}</p>
      <p>读论文要核对页码、图表及缺失来源；数据任务要回读产物并检查数值；文档任务要检查正文和预览。程序通过、模型报告和人工验收分别记录。</p>
      <details><summary>外部 agent 安装检查</summary>{result.external_agents.map((row:any)=><p key={row.id}>{row.id}：{row.found?"找到命令":"未找到命令"}。{row.note}；需要对应 CLI、账户和 API 权限。</p>)}</details>
      <p className="fine-print">{result.note}</p></div>}
  </section>;
}
