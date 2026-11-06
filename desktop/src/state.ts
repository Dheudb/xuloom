import type { EngineEvent, InitialSession, SessionView } from "./types.ts";

export function createSession(initial: InitialSession): SessionView {
  return {
    ...initial,
    permissionMode: initial.permissionMode || "ask",
    collaborationMode: initial.collaborationMode || "auto",
    busy: false,
    error: "",
    errorAction: undefined,
    messages: initial.history,
    streamIndex: null,
    tools: initial.activity || [],
    workers: initial.workers || [],
    commands: {},
    interactions: [],
    loaded: [],
    logs: [],
    selected: [],
    mode: "normal",
    draft: initial.savedDraft || "",
    editingId: initial.savedDraftEditId || undefined,
    attachments: initial.savedAttachments || [],
  };
}

export function beginTurn(session: SessionView, text: string): SessionView {
  return {
    ...session,
    busy: true,
    error: "",
    errorAction: undefined,
    draft: "",
    editingId: undefined,
    attachments: [],
    streamIndex: session.messages.length + 1,
    messages: [
      ...session.messages,
      { role: "user", text, ...(session.attachments.length ? { attachments: session.attachments } : {}) },
      { role: "assistant", text: "", trace: { id: "pending", startedAt: Date.now(), status: "running", steps: [] } },
    ],
  };
}

export function applyEvent(
  session: SessionView,
  event: EngineEvent,
): SessionView {
  if (event.session !== session.id) return session;
  switch (event.type) {
    case "user_input_queued":
      if (session.messages.some(message => message.id === event.input.id)) return session;
      return { ...session, pendingInputs: [...(session.pendingInputs || []).filter(item => item.id !== event.input.id), event.input] };
    case "user_input_withdrawn":
      return { ...session, pendingInputs: (session.pendingInputs || []).filter(item => item.id !== event.id) };
    case "user_input_applied": {
      const ids = new Set(event.inputs.map((item: {id:string}) => item.id));
      const inputs = event.inputs.filter((item: {id:string})=>!session.messages.some(message=>message.id===item.id));
      if (!inputs.length) return {...session,pendingInputs:(session.pendingInputs||[]).filter(item=>!ids.has(item.id))};
      return { ...session, busy: true, pendingInputs: (session.pendingInputs || []).filter(item => !ids.has(item.id)),
        streamIndex: session.messages.length + inputs.length,
        messages: [...session.messages, ...inputs.map((item: {id:string;text:string}) => ({role:'user' as const,id:item.id,text:item.text})),
          {role:'assistant',text:'',trace:event.trace || {id:'pending',startedAt:Date.now(),status:'running',steps:[]}}] };
    }
    case "goal_changed":
    case "goal_final":
      return { ...session, goal: event.goal };
    case "managed_turn_started":
      return beginTurn({ ...session, attachments: [] }, event.text);
    case "operation_started":
      return { ...session, busy: true, error: "" };
    case "operation_finished":
      return { ...session, busy: false, interactions: [] };
    case "operation_error":
      return { ...session, error: event.message };
    case "turn_restarted":
      return beginTurn({ ...session, messages: event.history, attachments: event.attachments || [] }, event.text);
    case "turn_resumed": {
      let index = -1;
      session.messages.forEach((message, at) => {
        if (message.role === "assistant" && message.trace?.id === event.trace.id) index = at;
      });
      if (index < 0) return session;
      return { ...session, busy: true, error: "", streamIndex: index,
        messages: session.messages.map((message, at) => at === index
          ? { ...message, trace: event.trace, text: event.trace.pendingText || event.trace.answer || "" } : message) };
    }
    case "command_status":
      return { ...session, commands: { ...session.commands, [event.command.id]: event.command } };
    case "file_changed":
      return { ...session, fileChanges: [event.change, ...(session.fileChanges || []).filter((file) => file.path !== event.change.path)].slice(0, 100) };
    case "permission_mode_changed":
      return ["ask", "full"].includes(event.permissionMode) ? { ...session, permissionMode: event.permissionMode } : session;
    case "collaboration_mode_changed":
      return ["solo", "auto", "ultra"].includes(event.collaborationMode) ? { ...session, collaborationMode: event.collaborationMode } : session;
    case "session_started":
      return {
        ...session,
        persistedId: event.sessionId,
        config: { ...session.config, model: event.model },
      };
    case "turn_trace":
      return session.streamIndex === null ? session : { ...session, messages: session.messages.map((message, index) =>
        index === session.streamIndex ? { ...message, trace: event.trace, text: event.trace.answer || event.trace.pendingText || "" }
          : index === session.streamIndex! - 1 && event.trace.userMessageId ? { ...message, id: event.trace.userMessageId } : message) };
    case "text_delta":
    case "final": {
      if (session.streamIndex === null) return session;
      const messages = session.messages.map((message, index) =>
        index === session.streamIndex
          ? {
              ...message,
              text:
                event.type === "final"
                  ? event.text || message.text
                  : message.text + (event.text || ""),
            }
          : message,
      );
      return { ...session, messages };
    }
    case "turn_finished":
      return { ...session, busy: false, streamIndex: null, messages: finishTrace(session, "completed") };
    case "turn_cancelled":
      return {
        ...session,
        busy: false,
        messages: finishTrace(session, "cancelled"),
        streamIndex: null,
        error: "任务已停止，已收到的内容保留。",
        errorAction: undefined,
        interactions: [],
        tools: settleTools(session, "cancelled"),
      };
    case "turn_error":
    case "engine_exit":
      return {
        ...session,
        commands: event.type === "engine_exit" ? Object.fromEntries(Object.entries(session.commands || {}).map(([id, command]) => [id, { ...command, status: command.status === "running" ? "interrupted" : command.status }])) : session.commands,
        workers: event.type === "engine_exit" ? session.workers.map(worker => ["running", "queued"].includes(worker.status) ? { ...worker, status: "interrupted" } : worker) : session.workers,
        busy: false,
        streamIndex: null,
        messages: finishTrace(session, event.type === "engine_exit" ? "interrupted" : "error").filter((message, index) =>
          index !== session.streamIndex || !!message.text.trim() || !!message.trace?.steps.length || !!message.trace && message.trace.id !== "pending"),
        error: (event.message || "引擎发生错误").replace(/\[\/?bold red\]/g, ""),
        errorAction: event.help?.action === "settings" ? "settings" : "activity",
        interactions: [],
        tools: settleTools(session, "interrupted", event.type === "engine_exit"),
      };
    case "permission":
    case "questions":
    case "plan_approval":
      return { ...session, interactions: [...session.interactions, event],
        tools: event.type === "permission" ? session.tools.map((tool) =>
          tool.id === `${event.request?.classifier_input?.agent_id && event.request.classifier_input.agent_role !== "main" ? event.request.classifier_input.agent_id : "main"}:${event.request?.classifier_input?.tool_use_id}`
            ? { ...tool, status: "waiting", waitingRequestId: event.requestId } : tool) : session.tools };
    case "interaction_resolved":
      return {
        ...session,
        interactions: session.interactions.filter(
          (item) => item.requestId !== event.requestId,
        ),
        tools: session.tools.map((tool) => tool.status === "waiting" && tool.waitingRequestId === event.requestId ? { ...tool, status: "running", waitingRequestId: undefined } : tool),
      };
    case "skill_loaded":
      return {
        ...session,
        loaded: [...new Set([...session.loaded, event.name])],
      };
    case "subagent_status": {
      const previous = session.workers.find(
        (item) => item.agent_id === event.agent.agent_id,
      );
      const worker = { ...previous, ...event.agent };
      const tools = upsertTool(session.tools, { id: "agent:" + worker.agent_id, name: "subagent",
        status: worker.status, agentId: worker.agent_id, agentRole: worker.agent_role || worker.agent_type,
        input: { description: worker.description, prompt: worker.task }, result: worker.result,
        startedAt: worker.started_at || session.tools.find((tool) => tool.id === "agent:" + worker.agent_id)?.startedAt || Date.now(),
        ...(!["running", "queued"].includes(worker.status) ? { finishedAt: worker.finished_at || Date.now() } : { finishedAt: undefined }) });
      return {
        ...session,
        workers: previous ? session.workers.map((item) => item.agent_id === worker.agent_id ? worker : item) : [...session.workers, worker],
        tools: tools.map((tool) => tool.agentId === worker.agent_id && !["running", "queued"].includes(worker.status) && ["running", "waiting", "queued"].includes(tool.status)
          ? { ...tool, status: worker.status === "cancelled" ? "cancelled" : "interrupted", finishedAt: Date.now() } : tool),
      };
    }
    case "tool_scheduled":
    case "tool_started": {
      const id = `${event.agent_id || "main"}:${event.tool_use_id || event.id}`;
      const previous = session.tools.find((tool) => tool.id === id);
      return { ...session, tools: upsertTool(session.tools, { id, name: event.tool_name || event.name || "tool",
        status: event.type === "tool_scheduled" ? previous?.status || "queued" : "running",
        agentId: event.agent_id, agentRole: event.agent_role,
        ...(event.input ? { input: event.input } : {}),
        startedAt: previous?.startedAt || event.timestamp || Date.now() }) };
    }
    case "tool_result":
      return { ...session, tools: upsertTool(session.tools, {
        id: `${event.agent_id || "main"}:${event.tool_use_id}`, name: event.tool_name || session.tools.find((tool) => tool.id === `${event.agent_id || "main"}:${event.tool_use_id}`)?.name || "tool",
        agentId: event.agent_id, agentRole: event.agent_role,
        status: event.status === "cancelled" ? "cancelled" : event.is_error ? "error" : "completed", finishedAt: event.timestamp || Date.now(),
        result: typeof (event.result ?? event.content) === "string" ? event.result ?? event.content : JSON.stringify(event.result ?? event.content ?? ""),
          media: event.media,
      }) };
    case "log":
      return {
        ...session,
        logs: [...session.logs, String(event.text).replace(/\[\/?(?:bold|dim|red|green|yellow|cyan|magenta|blue)(?:[ a-z]*)\]/g, "")].slice(-200),
      };
    case "sessions_changed":
      return { ...session, sessions: event.sessions };
    default:
      return session;
  }
}

function upsertTool(tools: SessionView["tools"], value: SessionView["tools"][number]) {
  const previous = tools.find((tool) => tool.id === value.id);
  return (previous ? tools.map((tool) => tool.id === value.id ? { ...tool, ...value } : tool) : [...tools, value]).slice(-200);
}
function finishTrace(session: SessionView, status: string) {
  return session.messages.map((message, index) => index === session.streamIndex && message.trace?.status === "running"
    ? { ...message, trace: { ...message.trace, status, finishedAt: Date.now(), elapsedMs: message.trace.resumedAt
      ? (message.trace.elapsedBeforeResumeMs || 0) + Date.now() - message.trace.resumedAt
      : Date.now() - (message.trace.startedAt || Date.now()) } } : message);
}
function settleTools(session: SessionView, status: string, includeChildren = true) {
  return session.tools.map((tool) => (includeChildren || !tool.agentId) && ["running", "queued", "waiting"].includes(tool.status)
    ? { ...tool, status, finishedAt: Date.now() } : tool);
}
