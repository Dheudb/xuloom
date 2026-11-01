import { useEffect, useState } from "react";
const labels: Record<string,string> = {paused:"已暂停",completed:"已完成",cancelled:"已取消",pending:"待投递",scheduled:"已计划"};
export default function SchedulePanel({invoke,onError}:{invoke:(method:string,params?:Record<string,unknown>)=>Promise<any>;onError:(error:unknown)=>void}) {
 const [jobs,setJobs]=useState<any[]>([]),[message,setMessage]=useState(""),[delay,setDelay]=useState(30),[daily,setDaily]=useState(false),[pending,setPending]=useState(false);
 const refresh=async()=>setJobs(await invoke("schedule.list"));
 useEffect(()=>{void refresh().catch(onError);const timer=setInterval(()=>void refresh().catch(onError),3000);return()=>clearInterval(timer);},[]);
 async function action(method:string,params:Record<string,unknown>={}){setPending(true);try{await invoke(method,params);await refresh();}catch(e){onError(e);}finally{setPending(false);}}
 return <div className="panel-scroll goal-panel">
    <><form className="goal-card" onSubmit={e => { e.preventDefault(); action("schedule.add", { prompt: message, due_at: Date.now() / 1000 + delay * 60, ...(daily ? { interval: 86400 } : {}) }); }}>
      <h4>稍后向此会话发送消息</h4><textarea required aria-label="定时消息" value={message} onChange={e => setMessage(e.target.value)} />
      <label>多少分钟后<input type="number" min="0" required value={delay} onChange={e => setDelay(Number(e.target.value))} /></label>
      <label><input type="checkbox" checked={daily} onChange={e => setDaily(e.target.checked)} />之后每 24 小时重复</label>
      <small>应用或 CLI 唤醒服务运行时投递；SSH 会话需保持连接并在工作台中打开，或在远端运行 CLI 唤醒服务。忙碌时排队，仍遵守会话审批。</small>
      <button type="submit" disabled={pending}>保存唤醒</button>
    </form>
    <h4>定时消息</h4>{jobs.map(job => <section key={job.id} className="goal-card"><strong>{job.prompt}</strong><p>{labels[job.status] || job.status} · {new Date(job.due_at * 1000).toLocaleString()}</p><small>{job.cwd}</small><p>{job.reason}</p>{!["completed", "cancelled"].includes(job.status) && <div className="goal-actions"><button disabled={pending} onClick={() => action("schedule.control", { id: job.id, action: job.status === "paused" ? "resume" : "pause" })}>{job.status === "paused" ? "恢复唤醒" : "暂停唤醒"}</button><button disabled={pending} onClick={() => action("schedule.control", { id: job.id, action: "cancel" })}>取消唤醒</button></div>}</section>)}</>
 </div>;
}
