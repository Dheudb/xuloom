import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Network, Plus, Play, Pause, Square, RefreshCw, Server, AlertTriangle, Send } from "lucide-react";
import Markdown from "./Markdown";
import RemoteSettings from "./RemoteSettings";
import { ReferenceNavigation } from "./ReferenceNavigation";
import "./scaling.css";

type Node = { id: string; ssh?: string | null; location?: "local" | "ssh"; max_agents: number; automatic: boolean; reserve_percent: number; connection?: string; model?: string; backend: string; api_source?: string; api_pool?: {connection: string; model?: string; backend?: string}[]; group_width?: number };
type ModelConnection = { connectionId: string; connectionName: string; model: string; models: string[] };
type Run = { id: string; goal: string; status: string; counts: Record<string, number>; target: number; round: number; report?: string; error?: string; completion_basis?: string; nodes: any[]; health?: any[]; groups?: { id: string; label: string }[] };
type ConversationMessage = {id:string; text:string; status:string; reply?:string; error?:string; applied_round?:number; nodes:Record<string,{status:string;error?:string}>};
type Task = { id: string; kind: string; label: string; status: string; remote_status?: string; node?: string; group?: string; session_id?: string; cwd?: string; error?: string; child_run_id?: string; child_counts?: Record<string, number>; receipt?: { text?: string; reason?: string; persisted?: boolean } };
const state: Record<string, string> = { running: "运行中", starting: "准备中", paused: "已暂停派发", blocked: "需要处理", completed: "模型报告完成", cancelling: "正在取消", cancelled: "已取消", queued: "排队中", dispatching: "确认派发中", failed: "失败", interrupted: "执行器中断", connected: "已连接", unconfirmed: "存活未确认" };
const empty = (ssh = "", location: "local" | "ssh" = "ssh"): Node => ({ id: location === "local" ? "local" : "", ssh, location, max_agents: 8, automatic: true, reserve_percent: 25, backend: "native", api_source: "inherit", api_pool: [], group_width: 50 });

export default function ScalingWorkbench({ close }: { close: () => void }) {
  const createKey = useRef(crypto.randomUUID());
  const messageKey = useRef({text:"",id:crypto.randomUUID()});
  const [messages,setMessages] = useState<ConversationMessage[]>([]), [messageText,setMessageText] = useState(""), [sending,setSending] = useState(false);
  const [entered, setEntered] = useState(false), [nodes, setNodes] = useState<Node[]>([]), [hosts, setHosts] = useState<any[]>([]);
  const [runs, setRuns] = useState<Run[]>([]), [active, setActive] = useState<Run>(), [tasks, setTasks] = useState<Task[]>([]), [questions, setQuestions] = useState<any[]>([]);
  const [form, setForm] = useState<Node>(), [sshOpen, setSshOpen] = useState(false), [checked, setChecked] = useState<Record<string, any>>({});
  const [goal, setGoal] = useState(""), [selected, setSelected] = useState<string[]>([]), [permission, setPermission] = useState("ask"), [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [detail, setDetail] = useState<Task>(), [offset, setOffset] = useState(0);
  const activeRun = useRef<string | undefined>(undefined); activeRun.current=active?.id;
  const [retryReviewed, setRetryReviewed] = useState(false);
  const [catalog, setCatalog] = useState<ModelConnection[]>([]), [leader, setLeader] = useState<Task>(), [children, setChildren] = useState<Task[]>([]), [childOffset, setChildOffset] = useState(0);
  useEffect(() => { setRetryReviewed(false); }, [detail?.id]);
  const call = (method: string, params: Record<string, unknown> = {}) => window.xuloom.invoke("scaling." + method, params);
  async function action(work: () => Promise<void>) { setBusy(true); setError(""); try { await work(); } catch (e) { setError(String(e)); } finally { setBusy(false); } }
  async function refresh() { const [cfg, list] = await Promise.all([call("config"), call("list")]); setNodes(cfg.nodes); setHosts(cfg.ssh); setCatalog(cfg.models || []); setRuns(list); }
  useEffect(() => { if (entered) void action(refresh); }, [entered]);
  useEffect(() => {
    if (!active) return;
    let stopped = false, timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [run, rows, pending, conversation] = await Promise.all([call("status", { id: active!.id }), call("tasks", { id: active!.id, offset }), call("questions", { id: active!.id }), call("messages", {id:active!.id})]);
        if (!stopped) { setActive(run); setTasks(rows); setQuestions(pending); setMessages(conversation); setRuns(previous => previous.map(r => r.id === run.id ? run : r)); }
      } catch (e) { if (!stopped) setError(String(e)); }
      if (!stopped) timer = setTimeout(poll, 1500);
    }
    void poll(); return () => { stopped = true; clearTimeout(timer); };
  }, [active?.id, offset]);
  async function inspectTask(task: Task, parent: Task | null | undefined = leader) {
    setDetail(task);
    await action(async () => {
      const result = await call(parent ? "child_result" : "result", { id: active!.id, task: parent?.id || task.id, child: parent ? task.id : undefined });
      setDetail(previous => previous?.id === task.id ? result : previous);
    });
  }
  useEffect(() => {
    const latest = (leader ? children : tasks).find(t => t.id === detail?.id);
    if (latest && latest.status !== detail?.status) void inspectTask(latest);
  }, [tasks, children, detail?.id, detail?.status]);
  useEffect(() => {
    if (!active || !leader) return;
    let stopped = false, timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { const rows = await call("children", {id: active!.id, task: leader!.id, offset: childOffset}); if (!stopped) setChildren(rows); } catch(e) { if (!stopped) setError(String(e)); }
      if (!stopped) timer = setTimeout(poll, 1500);
    }
    void poll(); return () => {stopped = true; clearTimeout(timer);};
  }, [active?.id, leader?.id, childOffset]);
  useEffect(() => {setMessages([]);setMessageText("");messageKey.current={text:"",id:crypto.randomUUID()};},[active?.id]);
  async function sendMessage() {
    if(!active || sending || !messageText.trim()) return;
    const runId=active.id, text=messageText.trim();
    if(messageKey.current.text!==text) messageKey.current={text,id:crypto.randomUUID()};
    setSending(true);setError("");
    try {
      const message=await call("message",{id:runId,text,request_id:messageKey.current.id});
      if(activeRun.current!==runId) return;
      setMessages(previous=>[...previous.filter(m=>m.id!==message.id),message]);
      setMessageText("");messageKey.current={text:"",id:crypto.randomUUID()};
    } catch(e) {if(activeRun.current===runId) setError(String(e));} finally {setSending(false);}
  }
  async function save(row: Node) { const value = await call("save", { nodes: [...nodes.filter(n => n.id !== row.id), row] }); setNodes(value); setForm(undefined); }
  function modelOptions(connection: string, current = "") {
    const item = catalog.find(c => c.connectionId === connection);
    return [...new Set([...(item?.models || []), item?.model || "", current].filter(Boolean))];
  }
  const total = nodes.filter(n => selected.includes(n.id)).reduce((sum, n) => sum + n.max_agents, 0);
  return <ReferenceNavigation.Provider value={undefined}><div className="scaling-workbench" role="dialog" aria-modal="true" aria-label="Scaling 蜂群工作台">
    <header><button onClick={close}><ArrowLeft size={17} /> 返回会话</button><strong><Network size={20} /> Scaling</strong><span>独立蜂群工作台 · <b className="scaling-beta">实验阶段</b></span></header>
    {!entered ? <section className="scaling-warning"><AlertTriangle size={32} /><h1>开启大规模探索</h1><p className="scaling-experimental-note">实验阶段／开发测试版：功能仍在开发与验证，暂不视为生产级集群调度。</p><p>Scaling 会主动拆分任务、多方向探索，并尽可能使用你设置的智能体容量。大量模型请求可能显著增加费用与主机负载。</p><p>可选择本机、SSH 节点或两者一起参与，默认沿用本机的 API 配置，由各执行主机直接请求供应商。资源探测用于保守调整并发，不是 CPU／内存的硬隔离；API 限流仍可能降低实际并发。</p><p>关闭此界面不会停止已接受的任务。请使用暂停或取消控制运行。</p><button className="primary" onClick={() => setEntered(true)}>了解用量，进入配置</button></section> : <div className="scaling-body">
      <aside className="scaling-navigation"><button onClick={() => { setActive(undefined); setDetail(undefined); setLeader(undefined); setOffset(0); void action(refresh); }}><Plus size={16} /> 新建蜂群</button><h3>运行记录</h3>{runs.map(run => <button className={active?.id === run.id ? "active" : ""} key={run.id} onClick={() => { setActive(run); setOffset(0); setDetail(undefined); setLeader(undefined); }}><span>{run.goal}</span><small>{state[run.status] || run.status}</small></button>)}<button onClick={() => void action(refresh)}><RefreshCw size={14} /> 刷新记录</button></aside>
      <main className="scaling-content">{!active ? <>
        <h1>配置探索集群</h1><p className="fine-print">本机与 SSH 节点都按组运行：每组一个独立负责人，默认最多带 50 个 worker。100 个 worker 对应 2 个组长；组长不计入 worker 数量。远端断线后继续执行与汇总。API 默认继承本机配置，也可为节点选择其他连接和模型；Skills、MCP 与产物保存在执行主机。普通会话的协作档位不受影响。</p>
        <section><div className="scaling-heading"><h2><Server size={18} /> 执行节点</h2><button disabled={busy || nodes.some(n => n.location === "local")} onClick={() => setForm(empty("", "local"))}><Plus size={15} /> 添加本机</button><button disabled={busy} onClick={() => setForm(empty(hosts[0]?.id))}><Plus size={15} /> 添加节点</button><button onClick={() => setSshOpen(!sshOpen)}>配置 SSH</button></div>
          {sshOpen && <RemoteSettings configurationOnly open={async () => {}} navigationAction={async () => {}} />}
          <button className="scaling-refresh-hosts" disabled={busy} onClick={() => void action(refresh)}>刷新 SSH 与节点列表</button>
          {!nodes.length && <p>尚未添加节点。可以添加本机，也可先配置 SSH 再添加远端节点。</p>}
          {nodes.map(node => <article className="scaling-node" key={node.id}><label><input type="checkbox" checked={selected.includes(node.id)} onChange={e => setSelected(e.target.checked ? [...selected, node.id] : selected.filter(id => id !== node.id))} /><strong>{node.id}</strong></label><span>{node.location === "local" ? "本机" : hosts.find(h => h.id === node.ssh)?.name || node.ssh} · worker 上限 {node.max_agents} · {Math.ceil(node.max_agents / (node.group_width || 50))} 个组长 · {node.automatic ? "自适应" : "手动"}</span><div><button disabled={busy} onClick={() => void action(async () => { const result = await call("check", { node }); setChecked(v => ({ ...v, [node.id]: result })); })}>检查节点</button><button onClick={() => setForm(node)}>编辑</button><button disabled={busy} onClick={() => void action(async () => { setNodes(await call("save", { nodes: nodes.filter(n => n.id !== node.id) })); setSelected(v => v.filter(id => id !== node.id)); })}>移除</button></div>{checked[node.id] && <small role="status">{checked[node.id].hostname} · {checked[node.id].resources.cpus} CPU · 当前建议 {checked[node.id].resources.suggested} 个 · {checked[node.id].binding.model}{checked[node.id].resources.warning ? " · " + checked[node.id].resources.warning : ""}</small>}</article>)}
          {form && <form className="scaling-form" onSubmit={e => { e.preventDefault(); void action(() => save(form)); }}><fieldset disabled={busy}><label>节点名称<input aria-label="Scaling 节点名称" required pattern="[A-Za-z0-9_-]+" maxLength={80} value={form.id} onChange={e => setForm({ ...form, id: e.target.value })} /></label>{form.location === "local" ? <label>执行位置<input aria-label="Scaling 执行位置" readOnly value="本机（无需 SSH）" /></label> : <label>SSH 连接<select aria-label="Scaling SSH 连接" required value={form.ssh || ""} onChange={e => setForm({ ...form, ssh: e.target.value })}><option value="">请选择</option>{hosts.map(h => <option key={h.id} value={h.id}>{h.name || h.id}</option>)}</select></label>}<label>worker 数量上限<input aria-label="Scaling 智能体上限" type="number" required min={1} max={2147483647} value={form.max_agents} onChange={e => setForm({ ...form, max_agents: Number(e.target.value) })} /></label><label>预留主机内存（%）<input type="number" min={10} max={80} required value={form.reserve_percent} onChange={e => setForm({ ...form, reserve_percent: Number(e.target.value) })} /></label><label className="scaling-checkbox"><input type="checkbox" checked={form.automatic} onChange={e => setForm({ ...form, automatic: e.target.checked })} />按主机资源自适应并发</label><label>每组 worker 上限<input aria-label="Scaling 分组宽度" type="number" min={1} max={1000} value={form.group_width || 50} onChange={e => setForm({...form, group_width: Number(e.target.value)})} /></label><label>模型配置来源<select aria-label="Scaling API 来源" value={form.api_source || "inherit"} onChange={e => setForm({...form, api_source: e.target.value})}><option value="inherit">继承本机当前连接</option><option value="local">选择其他连接与模型</option>{form.location !== "local" && <option value="remote">使用远端已保存配置</option>}</select></label><details className="scaling-model-options" open={form.api_source === "local"}><summary>模型与执行器</summary><div className="scaling-model-options-body"><label>执行器<select value={form.backend} onChange={e => setForm({...form, backend: e.target.value})}>{["native", "codex", "claude", "pi", "dsh"].map(b => <option key={b}>{b}</option>)}</select></label>{form.api_source === "local" ? <div className="scaling-model-pool">{(form.api_pool || []).map((entry, i) => <div className="scaling-model-row" key={i}><label>API 连接<select aria-label={`节点 API ${i+1}`} value={entry.connection} onChange={e => setForm({...form, api_pool: form.api_pool!.map((r,j) => j === i ? {...r, connection:e.target.value, model:catalog.find(c => c.connectionId === e.target.value)?.model || catalog.find(c => c.connectionId === e.target.value)?.models[0] || ""} : r)})}><option value="">请选择</option>{catalog.map(c => <option key={c.connectionId} value={c.connectionId}>{c.connectionName}</option>)}</select></label><label>模型<select aria-label={`节点模型 ${i+1}`} required value={entry.model || ""} onChange={e => setForm({...form, api_pool:form.api_pool!.map((r,j) => j === i ? {...r, model:e.target.value} : r)})}><option value="" disabled>请选择模型</option>{modelOptions(entry.connection, entry.model).map(model => <option key={model} value={model}>{model}</option>)}</select></label><button type="button" onClick={() => setForm({...form, api_pool:form.api_pool!.filter((_,j) => j !== i)})}>移除</button></div>)}<button className="scaling-add-model" type="button" disabled={(form.api_pool || []).length >= 64} onClick={() => setForm({...form, api_pool:[...(form.api_pool || []), {connection:catalog[0]?.connectionId || "", model:catalog[0]?.model || catalog[0]?.models[0] || ""}]})}><Plus size={15} />添加模型</button><small>连接在“设置 → 模型连接”中添加，保存后刷新列表。一个节点可使用多个 API 和模型。</small></div> : form.api_source === "remote" ? <><label>远端连接 ID（可选）<input value={form.connection || ""} placeholder="留空使用远端当前连接" onChange={e => setForm({...form, connection:e.target.value})} /></label><label>远端模型（可选）<input value={form.model || ""} onChange={e => setForm({...form, model:e.target.value})} /></label></> : null}<small>{form.location === "local" ? "本机复用已保存的 API 连接，独立分组会话在本机后台执行；外部执行器需在本机安装。" : "继承或选择本机连接时，所选凭据经 SSH 发送到该节点，按远端系统凭据保护策略保存；推理由远端直接请求供应商。外部执行器需在远端安装。"}</small></div></details><div className="scaling-form-actions"><button className="primary" type="submit">保存节点</button><button type="button" onClick={() => setForm(undefined)}>取消</button></div></fieldset></form>}
        </section><section><h2>探索目标</h2><label className="scaling-label">目标<textarea aria-label="Scaling 探索目标" rows={4} placeholder="描述你要探索的问题、约束与期望成果" value={goal} onChange={e => setGoal(e.target.value)} /></label><p>已选容量：<strong>{total}</strong> 个。先生成分组，再逐步填充独立任务；没有 250 或 1000 个的固定边界。</p><label className="scaling-label">工具权限<select aria-label="Scaling 工具权限" value={permission} onChange={e => setPermission(e.target.value)}><option value="ask">请求批准</option><option value="full">完全访问</option></select></label><label className="scaling-checkbox"><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} />我了解此运行可能产生大量 API 用量，并同意按所选节点容量探索</label><button className="primary" disabled={busy || !accepted || !goal.trim() || !total} onClick={() => void action(async () => { const run = await call("create", { goal, nodes: selected, permission, accept_high_usage: true, request_id: createKey.current }); createKey.current = crypto.randomUUID(); setActive(run); await refresh(); })}><Play size={15} /> 启动 Scaling</button></section>
      </> : <>
        <div className="scaling-heading"><h1>{active.goal}</h1><button disabled={busy || ["completed", "cancelled"].includes(active.status)} onClick={() => void action(async () => setActive(await call(active.status === "running" ? "pause" : "resume", { id: active.id })))}>{active.status === "running" ? <Pause size={15} /> : <Play size={15} />}{active.status === "running" ? "暂停派发" : "恢复"}</button><button disabled={busy || ["completed", "cancelled"].includes(active.status)} onClick={() => void action(async () => setActive(await call("cancel", { id: active.id })))}><Square size={14} /> 取消运行</button></div>
        <p role="status">{state[active.status] || active.status} · 第 {active.round} 轮 · 探索容量 {active.target}</p><code>{active.id}</code>{active.error && <p className="error">{active.error}</p>}
        <div className="scaling-counts">{Object.entries(active.counts).map(([key, count]) => <span key={key}>{state[key] || key}<b>{count}</b></span>)}</div>
        <div className="scaling-health">{active.health?.map(h => <span key={h.id}>{h.id.replace(active.id + "_", "")}：{state[h.status] || h.status}{h.resources ? ` · 当前建议 ${h.resources.suggested} · 运行 ${h.running}` : ""}{h.error && <small>{h.error}</small>}</span>)}</div>
        {active.status === "paused" && <p className="fine-print">已停止新派发；已开始的工具和模型请求可完成。取消运行才会向执行器发送停止请求。</p>}
        {questions.map(q => <section key={q.id} className="scaling-question"><h3>节点请求批准 · {q.node}</h3><pre>{JSON.stringify(q.payload, null, 2)}</pre><button disabled={busy} onClick={() => void action(async () => { await call("answer", { id: active.id, question: q.id, answer: "allow" }); })}>批准此次操作</button><button disabled={busy} onClick={() => void action(async () => { await call("answer", { id: active.id, question: q.id, answer: "deny" }); })}>拒绝</button></section>)}
        {active.report && <section><h2>总协调报告</h2><p className="fine-print">完成依据：模型报告。它不等于程序检查通过或人工验收。</p><Markdown text={active.report} allowImages={false} /></section>}
        <section className="scaling-conversation" aria-label="蜂群主 agent 对话"><h2>主 agent 对话</h2><p className="fine-print">可以询问进度、补充要求或调整探索方向。调整方向会停止旧方向中尚未完成的任务；已有结果和文件保留。断线节点恢复并确认后才启动新方向。</p>
          <div className="scaling-conversation-history" aria-live="polite">{messages.map(message=><article key={message.id}>
            <div className="scaling-owner-message">{message.text}</div>
            {message.reply && <div className="scaling-primary-message"><Markdown text={message.reply} allowImages={false}/></div>}
            <small role="status">{({queued:"已接收，等待主 agent",responding:"主 agent 正在处理",applying:"调整中，等待旧任务停止与节点确认",failed:"未应用",cancelled:"已取消"} as Record<string,string>)[message.status] || (message.applied_round ? `新方向已应用 · 第 ${message.applied_round} 轮` : "已回复")}</small>
            {Object.entries(message.nodes || {}).map(([node,delivery])=><small key={node}>{node}：{delivery.status==="pending" ? "等待节点同步" : "正在停止旧任务"}{delivery.error && ` · ${delivery.error}`}</small>)}
            {message.error && <p className="error">{message.error}</p>}
          </article>)}</div>
          <form className="scaling-message-form" onSubmit={e=>{e.preventDefault();void sendMessage();}}><textarea aria-label="给蜂群主 agent 发送消息" rows={2} placeholder="询问当前进度，或告诉主 agent 如何调整方向…" value={messageText} disabled={sending || ["cancelled","cancelling"].includes(active.status)} onChange={e=>setMessageText(e.target.value)} onKeyDown={e=>{if(e.key==="Enter" && !e.shiftKey && !e.nativeEvent.isComposing){e.preventDefault();void sendMessage();}}}/><button className="primary" type="submit" disabled={sending || !messageText.trim() || ["cancelled","cancelling"].includes(active.status)}><Send size={16}/>{sending ? "正在发送" : "发送给主 agent"}</button></form>
        </section>
        <section><h2>运行树与执行证据</h2><p className="fine-print">点击任务查看原始结果、执行主机与原生历史位置。断线节点不会被误报为任务已中断。</p><div className="scaling-tasks">{tasks.map(t => <button key={t.id} data-kind={t.kind} onClick={() => { if(t.kind === "leader") {setLeader(t); setChildren([]); setChildOffset(0); setDetail(undefined);} else {setLeader(undefined); void inspectTask(t, null);} }}><span>{t.group ? (active.groups?.find(g => g.id === t.group)?.label || "分组") + " / " : ""}{t.label}</span><small>{t.kind} · {t.node || "待分配"} · {t.remote_status === "queued" ? "节点排队中" : state[t.status] || t.status}</small></button>)}</div><div className="actions"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</button><span>{offset + 1}–{offset + tasks.length}</span><button disabled={tasks.length < 100} onClick={() => setOffset(offset + 100)}>下一页</button></div></section>
        {leader && <section className="scaling-nested"><div className="scaling-heading"><h2>{leader.label} → worker</h2><button onClick={() => {setLeader(undefined); setDetail(undefined);}}>返回总运行树</button><button onClick={() => void inspectTask(leader, null)}>节点汇总</button></div><div className="scaling-tasks">{children.map(t => <button key={t.id} data-kind={t.kind} onClick={() => void inspectTask(t, leader)}><span>{t.group ? "分组 / " : "分组负责人 / "}{t.label}</span><small>{t.kind} · {state[t.status] || t.status}</small></button>)}</div><div className="actions"><button disabled={!childOffset} onClick={() => setChildOffset(Math.max(0, childOffset - 100))}>上一页</button><span>{childOffset+1}–{childOffset+children.length}</span><button disabled={children.length < 100} onClick={() => setChildOffset(childOffset+100)}>下一页</button></div></section>}
        {detail && <section className="scaling-detail"><div className="scaling-heading"><h2>{detail.label}</h2><button onClick={() => setDetail(undefined)}>关闭详情</button></div><p>{detail.node} · {detail.session_id || "尚未创建历史"}</p><code>{detail.cwd}</code>{detail.error && <p className="error">{detail.error}</p>}<Markdown text={detail.receipt?.text || "尚未返回结果。"} allowImages={false} /></section>}
        {detail && !leader && ["failed", "interrupted"].includes(detail.status) && <section className="scaling-question"><p>重试会创建新的执行记录。先核对旧历史与文件，已有副作用不会自动撤销。</p><label className="scaling-checkbox"><input type="checkbox" checked={retryReviewed} onChange={e => setRetryReviewed(e.target.checked)} />我已检查旧结果，确认可以重试</label><button disabled={busy || !retryReviewed} onClick={() => void action(async () => { await call("retry", { id: active.id, task: detail.id, confirm_side_effects: true }); setDetail(undefined); setActive(await call("status", { id: active.id })); })}>重试此任务</button></section>}
      </>}{busy && <p role="status">处理中…</p>}{error && <p className="error" role="alert">{error}</p>}</main>
    </div>}
  </div></ReferenceNavigation.Provider>;
}
