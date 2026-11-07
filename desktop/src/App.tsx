import { useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  ArrowUpRight,
  Bot,
  Box,
  Check,
  ChevronRight,
  Workflow,
  Folder,
  FolderOpen,
  LoaderCircle,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Puzzle,
  Settings2,
  ShieldCheck,
  Square,
  X,
  Store,
  Pencil,
  Play,
  Paperclip,
  Activity,
  FolderTree,
  Target,
  Lightbulb,
} from "lucide-react";
import type { SessionView, ModelConfig, SessionCatalog, PermissionMode, FileChange, Attachment } from "./types";
import { applyEvent, beginTurn, createSession } from "./state";
import { ReasoningControls } from "./ReasoningControls";
import CapabilityPanel from "./CapabilityPanel";
import { InteractionCard, SettingsDialog } from "./Dialogs";
import Markdown from "./Markdown";
import { ReferenceNavigation } from "./ReferenceNavigation";
import type { FileReference } from "./references";
import MessageActions from "./MessageActions";
import ComposerMenu from "./ComposerMenu";
import ExecutionDialog from "./ExecutionDialog";
import SchedulePanel from "./SchedulePanel";
import GoalEditor from "./GoalEditor";
import ContextDialog from "./ContextDialog";
import CreateProjectDialog from "./CreateProjectDialog";
import GoalStrip from "./GoalStrip";
import GitStatus from "./GitStatus";
import SessionSummary from "./SessionSummary";
import RemoteReferences from "./RemoteReferences";
import ScalingWorkbench from "./ScalingWorkbench";
import McpDialog from "./McpDialog";
import SessionSidebar from "./SessionSidebar";
import { CatalogPreferences } from "./catalog-preferences";
import PanelResizer from "./PanelResizer";
import TurnProcess from "./TurnProcess";
import SkillInput from "./SkillInput";
import FileChanges, { relativeFile } from "./FileChanges";
import FilePreview from "./FilePreview";
import { clampPanelWidth, clampSidebarWidth, defaultSidebarWidth, DEFAULT_PANEL_WIDTH } from "./panel-width";
import useConversationScroll from "./useConversationScroll";
import Attachments, { AttachmentPreview } from "./Attachments";

const basename = (value: string) =>
  value.split(/[\\/]/).filter(Boolean).pop() || value;

export default function App() {
  const [sessions, setSessions] = useState<SessionView[]>([]);
  const [activeId, setActiveId] = useState("");
  const [catalog, setCatalog] = useState<SessionCatalog>({ projects: [], view: { sort: "recent" } });
  const preferences = useRef(new CatalogPreferences(catalog));
  const [defaultDirectory, setDefaultDirectory] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [scalingOpen, setScalingOpen] = useState(false);
  const [navigationRepair, setNavigationRepair] = useState(false);
  const [globalModel, setGlobalModel] = useState<ModelConfig>();
  const [modelChanging, setModelChanging] = useState(false);
  const [permissionChanging, setPermissionChanging] = useState(false);
  const [goalDialog, setGoalDialog] = useState<"goal" | "all" | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [collaborationChanging, setCollaborationChanging] = useState(false);
  const [executionDialog, setExecutionDialog] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState("skills");
  const [activityQuery, setActivityQuery] = useState("");
  const [panelWidth, setPanelWidth] = useState(DEFAULT_PANEL_WIDTH);
  const [filePanelWidth, setFilePanelWidth] = useState(700);
  const [reviewTreeWidth, setReviewTreeWidth] = useState(220);
  const [sidebarWidth, setSidebarWidth] = useState<number>();
  const [viewport, setViewport] = useState(window.innerWidth);
  const [opening, setOpening] = useState(true);
  const [notice, setNotice] = useState("");
  const [buildId, setBuildId] = useState("");
  const [appVersion, setAppVersion] = useState("");
  const [importing, setImporting] = useState(false);
  const [steeringSending, setSteeringSending] = useState(false);
  const steeringRequest = useRef<{session:string;text:string;id:string} | undefined>(undefined);
  const importOwner = useRef<{ id: string; remote: boolean } | null>(null);
  const [fileDrag, setFileDrag] = useState(false);
  const [attachmentPreview, setAttachmentPreview] = useState<{ name: string; source: string }>();
  const dragDepth = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const [filePreview, setFilePreview] = useState<{ path: string; change?: FileChange; initialView: "diff" | "content"; line?: number; column?: number; directory?: boolean; ownerId?: string }>();
  const input = useRef<HTMLTextAreaElement>(null);
  const navigation = useRef(0);
  const active = sessions.find((item) => item.id === activeId);
  const selectedModel = active?.remote ? active.config : globalModel;
  const conversationScroll = useConversationScroll(activeId, active?.messages, active?.interactions);
  const effectiveSidebarWidth = clampSidebarWidth(sidebarWidth ?? defaultSidebarWidth(viewport), viewport, panelOpen);
  const draftSessions = useRef(sessions);
  draftSessions.current = sessions;
  const navigateChat = useRef<(url: string) => void>(() => {});
  navigateChat.current = (url) => { void workspaceAction("chat.open", { url }).catch(onError); };
  const lastUser = active?.messages.slice().reverse().find((message) => message.role === "user");
  const paused = !active?.busy && ["cancelled", "interrupted"].includes(active?.messages.at(-1)?.trace?.status || "");
  const resumeAvailable = paused && !active?.draft.trim() && !active?.attachments.length && !active?.editingId && !active?.goalMode &&
    (!active?.goal || ["completed", "cancelled", "budget_exhausted"].includes(active.goal.status));
  function receiveCatalog(value: SessionCatalog) {
    setCatalog(preferences.current.receive(value));
  }

  useEffect(() => {
    const flush = (event: Event) => {
      if (window.xuloom?.flushDrafts(draftSessions.current.map((session) => ({ id: session.id, draft: session.draft, draftEditId: session.editingId || "", draftAttachments: session.attachments.map((item) => item.id) }))) === false) {
        event.preventDefault();
        if (event.type === "beforeunload") (event as BeforeUnloadEvent).returnValue = "";
        setNotice("草稿保存失败，窗口已保留。请检查存储空间、权限或会话占用，恢复后重试退出。");
      }
    };
    window.addEventListener("beforeunload", flush);
    window.addEventListener("xuloom-before-quit", flush);
    return () => { window.removeEventListener("beforeunload", flush); window.removeEventListener("xuloom-before-quit", flush); };
  }, []);
  useEffect(() => {
    if (!window.xuloom) {
      setNotice("请通过 Xuloom 桌面程序打开工作台。");
      return;
    }
    window.xuloom
      .invoke("app.initialize")
      .then((value) => {
        setGlobalModel(value.config);
        setBuildId(value.buildId || "");
        setAppVersion(value.version || "");
        setDefaultDirectory(value.workspaceSettings.defaultDirectory);
        if (Number.isFinite(value.layout?.inspectorWidth)) setPanelWidth(value.layout.inspectorWidth);
        if (Number.isFinite(value.layout?.filePreviewWidth)) setFilePanelWidth(value.layout.filePreviewWidth);
        if (Number.isFinite(value.layout?.reviewTreeWidth)) setReviewTreeWidth(value.layout.reviewTreeWidth);
        if (Number.isFinite(value.layout?.sidebarWidth)) setSidebarWidth(value.layout.sidebarWidth);
        receiveCatalog(value.catalog);
        setSessions((value.restored || []).map(createSession));
        setActiveId(value.activeId || "");
        if (value.restoreErrors?.length) setNotice(value.restoreErrors.join("\n"));
        return window.xuloom.invoke("app.ready");
      })
      .catch((error) => setNotice(String(error)))
      .finally(() => setOpening(false));
    return window.xuloom.onEvent((event) => {
      if (event.type === "chat_link") {
        navigateChat.current(event.url);
        return;
      }
      if (event.type === "session_loaded") {
        setSessions(previous => previous.some(session => session.id === event.initial.id) ? previous : [...previous, createSession(event.initial)]);
        return;
      }
      if (event.type === "settings_error") {
        setNotice(event.message);
        return;
      }
      if (event.type === "catalog_changed") {
        receiveCatalog(event.catalog);
        return;
      }
      if (event.type === "session_unloaded") {
        setSessions((previous) => previous.filter((session) => session.id !== event.session));
        return;
      }
      setSessions((previous) =>
        previous.map((session) => applyEvent(session, event)),
      );
    });
  }, []);
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth);
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  useEffect(() => {
    if (!active || opening || importing) return;
    const id = active.id, draft = active.draft, draftEditId = active.editingId || "", draftAttachments = active.attachments.map((item) => item.id);
    const timer = setTimeout(() => window.xuloom.invoke("session.update", { id, patch: { draft, draftEditId, draftAttachments } }).catch(onError), 500);
    return () => clearTimeout(timer);
  }, [activeId, active?.draft, active?.editingId, active?.attachments, opening, importing]);
  useEffect(() => {
    if (activeId) window.xuloom.invoke("session.activate", { session: activeId }).catch(onError);
    setGoalDialog(null);
    setExecutionDialog(false);
    setActivityQuery("");
    if (activeId) window.xuloom.invoke("goal.get", { session: activeId }).then((goal) => update({ goal }, activeId)).catch(onError);
  }, [activeId]);
  useEffect(() => {
    input.current?.focus();
    setFilePreview(previous => previous?.ownerId === activeId ? previous : undefined);
  }, [activeId, active?.project]);
  async function navigateReference(target: FileReference, remoteUrl?: string) {
    if (!active) return;
    const intent = navigation.current + (remoteUrl ? 1 : 0);
    let owner = active.id;
    if (remoteUrl) { const loaded = await workspaceAction("chat.open", { url: remoteUrl }); if (!loaded) return; owner = loaded.activeId || loaded.existingId || loaded.id; }
    if (intent !== navigation.current) return;
    const info = await window.xuloom.invoke("files.reference", { session: owner, path: target.path, basePath: target.basePath }).catch(error => { if (remoteUrl) onError(error); throw error; });
    if (intent !== navigation.current) return;
    setFilePreview({ path: info.path, initialView: "content", directory: info.directory, line: target.line, column: target.column, ownerId: owner });
    setPanelOpen(true);
  }
  function openFile(path: string, change?: FileChange) {
    if (!active) return;
    const initialView = change ? "diff" : "content";
    change ||= active.fileChanges?.find((file) => file.path === path || relativeFile(file.path, active.project) === path.replace(/^\.[\\/]/, ""));
    setFilePreview({ path, change, initialView, ownerId: active.id });
    setPanelOpen(true);
  }
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        if (!opening && !settingsOpen && !mcpOpen && !createProjectOpen && !scalingOpen) openSession();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });

  function update(patch: Partial<SessionView>, id = activeId) {
    setSessions((previous) =>
      previous.map((session) =>
        session.id === id ? { ...session, ...patch } : session,
      ),
    );
  }
  function onError(error: unknown) {
    setNotice(
      String(error).replace(
        /^Error: Error invoking remote method '[^']+': Error: /,
        "",
      ),
    );
  }
  async function invoke(method: string, params: Record<string, unknown> = {}) {
    return window.xuloom.invoke(method, { session: activeId, ...params });
  }
  async function workspaceAction(method: string, params: Record<string, unknown>) {
    const preferenceWrite = method === "catalog.view" ? preferences.current.begin(params as Partial<SessionCatalog["view"]>) : undefined;
    if (preferenceWrite) setCatalog(preferenceWrite.catalog);
    const changesSelection = !["chat.copy", "chat.register", "chat.protocol"].includes(method) && !method.startsWith("catalog.") && !method.startsWith("archive.") &&
      (method !== "session.update" || !!(params.patch as { archived?: boolean } | undefined)?.archived) &&
      (method !== "project.update" || !!(params.patch as { archived?: boolean } | undefined)?.archived);
    const intent = changesSelection ? ++navigation.current : navigation.current;
    if (changesSelection) setOpening(true);
    setNotice("");
    try {
      if (active && ["remote.open", "chat.open", "project.open", "project.create", "session.new", "session.open", "session.select", "session.move", "session.fork"].includes(method))
        await window.xuloom.invoke("session.update", { id: active.id, patch: { draft: active.draft, draftEditId: active.editingId || "", draftAttachments: active.attachments.map((item) => item.id) } });
      const result = await window.xuloom.invoke(method, params);
      if (!result) return;
      if (result.notice) setNotice(result.notice);
      if (method === "chat.copy") setNotice("聊天链接已复制");
      if (preferenceWrite) setCatalog(preferences.current.finish(preferenceWrite.id, result));
      else if (result.catalog) receiveCatalog(result.catalog);
      else if (method.startsWith("catalog.")) receiveCatalog(result);
      if (result.id) {
        // A session_loaded event can precede this reply and already receive
        // live output. Never replace that state with its older initial view.
        setSessions((previous) => {
          const kept = previous.filter(session => session.id !== result.movedFrom);
          return kept.some(session => session.id === result.id) ? kept : [...kept, createSession(result)];
        });
      }
      if (changesSelection && intent === navigation.current) {
        if ("activeId" in result) setActiveId(result.activeId);
        else if (result.existingId || result.id) setActiveId(result.existingId || result.id);
      }
      return result;
    } catch (error) {
      if (preferenceWrite) {
        setCatalog(preferences.current.fail(preferenceWrite.id));
        window.xuloom.invoke("catalog.refresh").then(receiveCatalog).catch(() => {});
      }
      throw error;
    } finally {
      if (changesSelection && intent === navigation.current) setOpening(false);
    }
  }
  async function openProject(project?: string) {
    if (!project) { setCreateProjectOpen(true); return; }
    try {
      await workspaceAction("project.open", { path: project });
    } catch (error) {
      onError(error);
    }
  }
  async function openSession(resumeId?: string) {
    try {
      if (resumeId && active) await workspaceAction("session.open", { session: activeId, resumeId });
      else await workspaceAction("session.new", {});
    } catch (error) {
      onError(error);
    }
  }
  async function refresh() {
    if (active) update({ capabilities: await invoke("capabilities.list") });
  }
  function editLastPrompt() {
    if (!active || active.busy || !lastUser?.id) return;
    update({ editingId: lastUser.id, draftBeforeEdit: active.draft, draft: lastUser.text, attachmentsBeforeEdit: active.attachments, attachments: lastUser.attachments || [] });
    requestAnimationFrame(() => input.current?.focus());
  }
  async function branchFrom(index: number) {
    if (!active || active.busy || opening) return;
    const message = active.messages[index];
    const user = active.messages.slice(0, index).reverse().find((item) => item.role === "user");
    await workspaceAction("session.fork", { id: active.id, through: {
      turn: active.messages.slice(0, index + 1).filter((item) => item.role === "assistant").length,
      text: message.text, userId: message.trace?.userMessageId || user?.id, traceId: message.trace?.id,
    } });
  }
  function composerAction(name: string) {
    if (name === "context") { setContextOpen(true); return; }
    if (name === "isolation") { setExecutionDialog(true); return; }
    if (name === "files") fileInput.current?.click();
    else if (name === "goal") {
      if (active?.goal && !["completed", "cancelled", "budget_exhausted"].includes(active.goal.status)) { setGoalDialog("goal"); return; }
      if (active?.attachments.length || active?.editingId) { onError("请先发送附件或结束提示词编辑，再选择目标模式。"); return; }
      update({ goalMode: true, mode: "normal" });
      requestAnimationFrame(() => input.current?.focus());
    } else if (name === "schedule") {
      if (active?.attachments.length) { onError("请先发送附件，或移除附件后设置目标与唤醒。"); return; }
      setGoalDialog("all");
    } else if (name === "plan") {
      if (active?.goal && !["completed", "cancelled", "budget_exhausted"].includes(active.goal.status)) { onError("请先结束当前目标，再切换计划模式。"); return; }
      update({ mode: active?.mode === "plan" ? "normal" : "plan", goalMode: false });
    } else if (name === "skills") { setPanelOpen(true); setPanelTab("skills"); }
    else if (name === "mcp") setMcpOpen(true);
    else if (name === "settings") void openSettings();
  }
  async function resume() {
    if (!resumeAvailable || !active || opening || importing || modelChanging || permissionChanging || collaborationChanging) return;
    const id = active.id;
    conversationScroll.followLatest();
    update({ busy: true, error: "" }, id);
    try {
      await window.xuloom.invoke("prompt.resume", { session: id });
    } catch (error) {
      update({ busy: false, error: String(error) }, id);
    }
  }
  async function appendInput() {
    if (!active || !active.busy || !active.steering || steeringSending || !active.draft.trim()) return;
    const id=active.id,text=active.draft.trim();
    if (steeringRequest.current?.session !== id || steeringRequest.current?.text !== text)
      steeringRequest.current={session:id,text,id:crypto.randomUUID()};
    setSteeringSending(true);
    try {
      const receipt=await window.xuloom.invoke('prompt.append',{session:id,text,requestId:steeringRequest.current.id});
      setSessions(previous=>previous.map(s=>s.id===id?{...s,error:'',draft:s.draft.trim()===text?'':s.draft,
        ...(receipt.status==='queued' && !s.messages.some(m=>m.id===receipt.id)?{pendingInputs:[...(s.pendingInputs||[]).filter(item=>item.id!==receipt.id),receipt]}:{})}:s));
      steeringRequest.current=undefined;
    } catch(error) { update({error:String(error),errorAction:undefined},id); }
    finally { setSteeringSending(false); }
  }
  async function send() {
    if (active?.controlledExecution) return;
    if (active?.busy && active.steering) { await appendInput(); return; }
    if (resumeAvailable) { await resume(); return; }
    if (active?.pendingInputs?.length && !active.busy) { onError('还有未送达的补充要求，请继续原任务，或先取消这些消息再开始新任务。'); return; }
    if (opening || importing || modelChanging || permissionChanging || collaborationChanging || !active || active.busy || (!active.draft.trim() && !active.attachments.length)) return;
    if (active.attachments.some(item => item.available === false)) { onError("附件上传未完成；请移除后重新添加，再发送任务。"); return; }
    if (active.goalMode) {
      if (active.attachments.length || active.editingId) { onError("目标模式请直接输入目标；附件或提示词编辑请先单独发送。"); return; }
      const id = active.id, objective = active.draft.trim();
      update({ busy: true, error: "" }, id);
      let created = false;
      try {
        const goal = await window.xuloom.invoke("goal.create", { session: id, objective, criteria: [objective], skills: active.selected, limits: active.goalLimits });
        created = true;
        update({ goal, goalMode: false, mode: "normal", draft: "" }, id);
        conversationScroll.followLatest();
        await window.xuloom.invoke("goal.run", { session: id });
      } catch (error) {
        update({ busy: false, error: String(error), ...(!created ? { draft: objective, goalMode: true } : {}) }, id);
      }
      return;
    }
    conversationScroll.followLatest();
    const id = active.id,
      text = active.draft.trim() || "请查看这些附件。",
      selected = active.selected,
      mode = active.mode,
      attached = active.attachments,
      replaceMessageId = active.editingId;
    setSessions((previous) =>
      previous.map((session) =>
        session.id === id ? replaceMessageId ? { ...session, busy: true, error: "" } : beginTurn(session, text) : session,
      ),
    );
    try {
      await window.xuloom.invoke("prompt", {
        session: id,
        text,
        skills: selected,
        mode,
        replaceMessageId,
        attachments: attached.map((item) => item.id),
      });
    } catch (error) {
      setSessions((previous) =>
        previous.map((session) =>
          applyEvent(session.id === id ? { ...session, draft: text, editingId: replaceMessageId, attachments: attached } : session, {
            session: id,
            type: "turn_error",
            message: String(error),
          }),
        ),
      );
    }
  }
  async function importFiles(files: File[]) {
    if (active?.controlledExecution) { onError("此子代理只读；请回原主会话添加资料。"); return; }
    if (!files.length) return;
    if (importing || opening) { onError(importing ? "正在添加附件，请稍后再添加" : "会话正在打开，请稍后再添加附件"); return; }
    if (active?.busy) { onError("请先停止当前任务，再添加附件"); return; }
    if (active?.remote && !active.remote.attachments) { onError("请先更新远端 Xuloom，再传输附件。"); return; }
    setImporting(true);
    let id = activeId;
    const remote = !!active?.remote;
    try {
      const existing = active?.attachments.map((item) => item.id) || [];
      if (existing.length + files.length > 10) throw new Error("每条消息最多添加 10 个附件");
      if (!active) {
        const value = await window.xuloom.invoke("session.new", { scope: "standalone" });
        if (value.catalog) receiveCatalog(value.catalog);
        setSessions((previous) => [...previous, createSession(value)]);
        id = value.id; setActiveId(id);
      }
      importOwner.current = { id, remote };
      const pending: Attachment[] = remote ? files.map(file => ({ id: crypto.randomUUID(), name: file.name || "粘贴图片", path: "", size: file.size,
        kind: /\.(png|jpe?g|webp|gif|bmp)$/i.test(file.name) ? "image" : "file", available: false, state: "uploading" })) : [];
      if (pending.length) setSessions(previous => previous.map(session => session.id === id ? { ...session, attachments: [...session.attachments, ...pending] } : session));
      const items = await window.xuloom.importFiles(id, files, existing, remote ? pending.map(item => item.id) : undefined);
      setSessions((previous) => previous.map((session) => session.id === id ? { ...session, attachments: items } : session));
    } catch (error) {
      if (remote && id) {
        try {
          const saved = await window.xuloom.invoke("attachments.list", { session: id });
          setSessions(previous => previous.map(session => session.id === id ? { ...session, attachments: saved.items } : session));
        } catch { /* Disconnected transfers retain their IDs for the next explicit reopen. */ }
      }
      onError(error);
    }
    finally { importOwner.current = null; setImporting(false); }
  }
  async function openAttachment(item: Attachment) {
    try {
      if (item.kind === "image") setAttachmentPreview(await invoke("attachments.preview", { id: item.id }));
      else await invoke("attachments.open", { id: item.id });
    } catch (error) { onError(error); }
  }
  function applyModel(config: ModelConfig) {
    const remoteKey = active?.remote?.key;
    if (!remoteKey) setGlobalModel(config);
    setSessions(previous => previous.map(session => session.busy || (remoteKey ? session.remote?.key !== remoteKey : !!session.remote) ? session : { ...session, config }));
  }
  async function saveModel(value: ModelConfig & { apiKey: string }) {
    const config = await invoke("config.save", value);
    applyModel(config);
  }
  async function selectModel(value: string) {
    if (modelChanging) return;
    setModelChanging(true);
    try {
      const [connectionId, model] = JSON.parse(value);
      const config = await invoke("config.select", { connectionId, model });
      applyModel(config);
    } catch (error) { onError(error); }
    finally { setModelChanging(false); }
  }
  async function selectReasoning(value: { thinking?: "default" | "enabled" | "disabled"; effort?: string }) {
    if (!selectedModel || modelChanging) return;
    setModelChanging(true);
    try {
      const config = await invoke("config.reasoning", { connectionId: selectedModel.connectionId, model: selectedModel.model, ...value });
      applyModel(config);
    } catch (error) { onError(error); }
    finally { setModelChanging(false); }
  }
  async function setPermissionMode(mode: PermissionMode) {
    if (!active || permissionChanging) return;
    const id = active.id;
    setPermissionChanging(true);
    try {
      const result = await window.xuloom.invoke("permissions.set", { session: id, mode });
      update({ permissionMode: result.permissionMode }, id);
    } catch (error) { onError(error); }
    finally { setPermissionChanging(false); }
  }
  async function setCollaborationMode(mode: SessionView["collaborationMode"]) {
    if (!active || collaborationChanging) return;
    const id = active.id;
    setCollaborationChanging(true);
    try {
      const result = await window.xuloom.invoke("collaboration.set", { session: id, mode });
      update({ collaborationMode: result.collaborationMode }, id);
    } catch (error) { onError(error); }
    finally { setCollaborationChanging(false); }
  }
  async function openSettings() {
    try {
      const [config, workspace] = await Promise.all([invoke("config.get"), window.xuloom.invoke("workspace.get")]);
      applyModel(config);
      setDefaultDirectory(workspace.defaultDirectory);
      setSettingsOpen(true);
    } catch (error) {
      onError(error);
    }
  }
  const title = catalog.projects.flatMap((project) => project.sessions).find((row) => row.id === activeId)?.title ||
    active?.messages
      .find((message) => message.role === "user")
      ?.text.split("\n")[0] || "新会话";
  const enabledSkills =
    active?.capabilities.skills.filter(
      (skill) => skill.valid && skill.enabled && !skill.shadowed,
    ) || [];
  const runningWorkers =
    active?.workers.filter((worker) => worker.status === "running" || worker.status === "queued" && worker.phase !== "awaiting_resume").length || 0;
  const runningCommands = Object.values(active?.commands || {}).filter((command) => command.status === "running").length;

  return (
    <ReferenceNavigation.Provider value={navigateReference}>
    <div className={"workbench " + (!panelOpen ? "panel-hidden" : "")} style={{ "--sidebar-width": effectiveSidebarWidth + "px", "--inspector-width": clampPanelWidth(filePreview ? filePanelWidth : panelWidth, viewport, effectiveSidebarWidth) + "px" } as React.CSSProperties}
      onDragEnter={(event) => { if (Array.from(event.dataTransfer.types).includes("Files")) { event.preventDefault(); dragDepth.current++; setFileDrag(true); } }}
      onDragLeave={() => { if (--dragDepth.current <= 0) { dragDepth.current = 0; setFileDrag(false); } }}
      onDragOver={(event) => { if (Array.from(event.dataTransfer.types).includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = active?.busy || importing ? "none" : "copy"; } }}
      onDrop={(event) => { if (Array.from(event.dataTransfer.types).includes("Files")) { event.preventDefault(); event.stopPropagation(); dragDepth.current = 0; setFileDrag(false); void importFiles(Array.from(event.dataTransfer.files)); } }}>
      {fileDrag && <div className="file-drop-overlay"><Paperclip size={28} /><strong>{active?.busy ? "请先停止当前任务" : "松开以添加附件"}</strong><span>图片、PDF 和本地文件</span></div>}
      <aside className="sidebar">
        <PanelResizer side="left" width={effectiveSidebarWidth} viewport={viewport} panelOpen={panelOpen} change={setSidebarWidth}
          save={(sidebarWidth) => { void window.xuloom.invoke("layout.save", { sidebarWidth }).catch(onError); }} />
        <div className="brand">
          <span className="brand-mark">
            <Workflow size={21} />
          </span>
          <strong>
            Xuloom<span>序织</span>
          </strong>
        </div>
        <button
          className="new-task"
          disabled={opening}
          onClick={() => openSession()}
        >
          <Plus size={17} />
          新建会话<span>Ctrl N</span>
        </button>
        <button
          className="sidebar-action"
          disabled={opening}
          onClick={() => openProject()}
        >
          <FolderOpen size={16} />
          创建项目{opening && <LoaderCircle size={14} className="spin" />}
        </button>
        <SessionSidebar catalog={catalog} activeId={activeId} live={sessions} opening={opening}
          action={workspaceAction} openProject={openProject} onError={onError} />
        <div className="sidebar-bottom">
          <button className="sidebar-action" aria-label="Scaling 蜂群" onClick={() => setScalingOpen(true)}><Workflow size={16} />Scaling 蜂群 <span className="scaling-beta">实验阶段</span></button>
          <button
            className="sidebar-action"
            title={active ? "为当前工作区管理外部工具" : "浏览 MCP 服务目录"}
            onClick={() => setMcpOpen(true)}
          >
            <Store size={16} />
            MCP 市场
          </button>
          <div className="local-status">
            <span />
            本地工作区<span className="version" title={buildId ? "构建 " + buildId : "开发版本"}>{appVersion ? "v" + appVersion : "开发版"}{buildId ? " · " + buildId.slice(-4) : ""}</span>
          </div>
          <button className="sidebar-action" onClick={openSettings}>
            <Settings2 size={16} />
            设置与模型
          </button>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs">
            <Folder size={15} />
            <button
              title={active?.project}
              onClick={() => { if (active?.remote) { setFilePreview(undefined); setPanelTab("files"); setPanelOpen(true); } else if (active) void invoke("project.reveal").catch(onError); }}
            >
              {active?.remote ? `${active.remote.name} · ${active.remote.host}` : active?.scope === "standalone" ? "独立会话" : active ? catalog.projects.find((project) => project.path.toLowerCase() === active.project.toLowerCase())?.name || basename(active.project) : "工作台"}
            </button>
            <ChevronRight size={13} />
            <span>{active ? title.slice(0, 35) : "开始"}</span>
          </div>
          <div className="topbar-actions">
            {active && <SessionSummary key={active.id} sessionId={active.id} busy={active.busy} openFile={openFile}
              openActivity={tool => { setActivityQuery(tool || ""); setFilePreview(undefined); setPanelTab("activity"); setPanelOpen(true); }} />}
            {active && <GitStatus key={active.id + active.project} sessionId={active.id} project={active.project} busy={active.busy} />}
            <span className="local-tag">
              <span />
              {active?.remote ? `SSH · ${active.remote.hostname || active.remote.host}${active.busy ? " · 执行中" : ""}` : active?.busy ? "执行中" : "本地"}
            </span>
            <div className="panel-shortcuts" aria-label="会话工具">
              {[
                { id: "files", label: "文件", icon: FolderTree },
                { id: "activity", label: "活动", icon: Activity },
              ].map(({ id, label, icon: Icon }) => (
                <button key={id} className="icon-button" title={label} aria-label={"打开" + label + "面板"}
                  aria-pressed={panelOpen && !filePreview && panelTab === id}
                  onClick={() => { setActivityQuery(""); setFilePreview(undefined); setPanelTab(id); setPanelOpen(true); }}>
                  <Icon size={17} />
                </button>
              ))}
            </div>
            <button
              className="icon-button"
              title={panelOpen ? "收起侧栏" : "技能、子代理与会话工具"}
              aria-label={panelOpen ? "收起能力面板" : "展开能力面板"}
              aria-expanded={panelOpen}
              onClick={() => setPanelOpen(!panelOpen)}
            >
              {panelOpen ? (
                <PanelRightClose size={18} />
              ) : (
                <PanelRightOpen size={18} />
              )}
            </button>
          </div>
        </header>
        {!!catalog.navigationIssues && <div className="global-notice" role="alert"><span>{catalog.navigationIssues} 项 SSH 导航数据异常，本机历史可继续使用。</span><button onClick={() => setNavigationRepair(true)}>处理 SSH 导航问题</button></div>}
        {notice && (
          <div className="global-notice" role="alert">
            <span>{notice}</span>
            <button aria-label="关闭提示" onClick={() => setNotice("")}>
              <X size={15} />
            </button>
          </div>
        )}
        <div className="conversation" ref={conversationScroll.ref} onScroll={conversationScroll.onScroll} onWheel={conversationScroll.onWheel}>
          {!active || !active.messages.length ? (
            <div className="welcome">
              <div className="welcome-symbol">
                <Workflow size={32} />
              </div>
              <div className="eyebrow">YOUR LOCAL AGENT WORKSPACE</div>
              <h1>
                {active?.scope === "standalone" ? "开始一个新会话。" : active ? "今天想完成什么？" : "从一个问题或任务开始。"}
              </h1>
              <p>
                {active?.scope === "standalone" ? "直接开始对话；文件和命令任务使用此会话的专属工作目录。" : active
                  ? "描述你的任务，连接模型，让代理在这个项目里开始工作。"
                  : "连接模型即可开始；每个独立会话都有自己的工作目录，也可以打开项目资料。"}
              </p>
              {!active ? (
                <button
                  className="primary open-project"
                  disabled={opening}
                  onClick={() => openSession()}
                >
                  <FolderOpen size={17} />
                  {opening ? "正在打开…" : "开始新会话"}
                  <ArrowUpRight size={15} />
                </button>
              ) : (
                <>
                  <div className="project-context">
                    <Folder size={14} />
                    <span title={active.project}>{active.project}</span>
                  </div>
                  <div className="starter-grid">
                    {[
                      {
                        icon: Paperclip,
                        title: "阅读与总结资料",
                        text: "提炼要点，保留来源和未读内容。",
                        prompt: "请帮我总结资料，区分原文事实与推测，标明来源和无法读取的内容。先确认要处理的文件或链接。",
                      },
                      {
                        icon: Workflow,
                        title: "分析表格与数据",
                        text: "核对数据口径，生成可复现结果。",
                        prompt: "请帮我分析数据。先确认数据来源和分析问题，检查格式、字段及本机工具支持，保留原始数据，并验证关键计算。",
                      },
                      {
                        icon: Pencil,
                        title: "撰写或修改文档",
                        text: "围绕读者、用途与已有材料写作。",
                        prompt: "请帮我撰写或修改文档。先确认读者、用途、材料和输出格式；需要保存时明确产物位置，不编造事实和引用。",
                      },
                    ].map(({ icon: Icon, title, text, prompt }) => (
                      <button
                        key={title}
                        onClick={() => update({ draft: prompt })}
                      >
                        <Icon size={19} />
                        <strong>{title}</strong>
                        <span>{text}</span>
                        <ArrowUpRight size={14} />
                      </button>
                    ))}
                  </div>
                </>
              )}
              {!active && (
                <div className="welcome-features">
                  <span>
                    <ShieldCheck size={14} />
                    项目保存在本地
                  </span>
                  <span>
                    <Puzzle size={14} />
                    按需挂载 Skills
                  </span>
                  <span>
                    <Bot size={14} />
                    独立子代理
                  </span>
                </div>
              )}
            </div>
          ) : (
            <div className="transcript" onDragStart={(event) => {
              if ((event.target as Element).closest(".message-text")) event.preventDefault();
            }} onMouseDown={(event) => {
              const target = event.target as Element;
              if (event.button === 0 && event.detail === 1 && !event.shiftKey && !event.ctrlKey && !event.metaKey &&
                target.closest(".message-text") && !target.closest("button, a, input, textarea")) {
                // Starting a fresh selection must not turn old selected text into
                // an HTML drag operation. Double/triple-click and Shift selection remain native.
                const selected = window.getSelection();
                if (selected && !selected.isCollapsed) selected.removeAllRanges();
              }
            }}>
              {active.messages.map((message, index) => (
                <article key={index} className={"message " + message.role} aria-label={message.role === "user" ? "你的消息" : "Xuloom 回复"}>
                  <div className="message-avatar">
                    {message.role === "assistant" ? <Workflow size={16} /> : "你"}
                  </div>
                  <div className="message-body">
                    <div className="message-author">
                      {message.role === "assistant" ? "Xuloom" : "你"}
                      {message.role === "assistant" &&
                        active.busy &&
                        index === active.streamIndex && (
                          <span className="working-label">
                            <span />
                            处理中
                          </span>
                        )}
                    </div>
                    {message.trace && <TurnProcess trace={message.trace} sessionId={activeId} />}
                    <div className="markdown message-text">
                      {message.text ? (
                        message.role === "user" ? <div className="user-prompt-text">{message.text}</div> : <Markdown text={message.text} onOpenFile={openFile} sessionId={activeId} />
                      ) : active.busy && index === active.streamIndex ? (
                        <div className="thinking">
                          <span />
                          <span />
                          <span />
                        </div>
                      ) : !message.trace ? (
                        <span className="muted">此次任务未返回文本。</span>
                      ) : null}
                    </div>
                    {!!message.attachments?.length && <Attachments items={message.attachments} open={openAttachment} remote={!!active?.remote} />}
                    {!!message.trace?.files?.length && <FileChanges files={message.trace.files} project={active.project} onOpen={openFile}
                      onUndo={!active.busy && !runningWorkers && !runningCommands ? async (file) => {
                        await window.xuloom.invoke("files.undo", { session: active.id, revisionId: file.revisionId, confirm: true });
                        setFilePreview(undefined);
                      } : undefined} />}
                    {!!message.text && <MessageActions key={active.id + ":" + index} role={message.role} text={message.text} onError={onError}
                      fork={message.role === "assistant" ? () => branchFrom(index) : undefined}
                      disabled={opening || active.busy || !!runningWorkers || !!runningCommands}>
                      {message === lastUser && message.id && <button aria-label="编辑上一条提示词" title="编辑上一条提示词（空输入框连按两次 Esc）"
                        disabled={opening || active.busy || !!runningWorkers || !!runningCommands} onClick={editLastPrompt}><Pencil size={13} /></button>}
                    </MessageActions>}
                  </div>
                </article>
              ))}
            </div>
          )}
          {active?.interactions.map((interaction) => (
            <InteractionCard
              key={interaction.requestId}
              interaction={interaction}
              respond={(requestId, answer) =>
                invoke("respond", { requestId, answer })
              }
            />
          ))}
        </div>
        <div className="composer-area">
          {active?.error && (!paused || active.error !== "任务已停止，已收到的内容保留。") && (
            <div className="session-error" role="alert">
              {active.error}
              {active.errorAction && <button onClick={() => {
                if (active.errorAction === "settings") setSettingsOpen(true);
                else { setFilePreview(undefined); setPanelTab("activity"); setPanelOpen(true); }
              }}>{active.errorAction === "settings" ? "检查模型设置" : "查看活动"}</button>}
              <button
                aria-label="清除会话提示"
                onClick={() => update({ error: "" })}
              >
                <X size={13} />
              </button>
            </div>
          )}
          {active && runningWorkers > 0 && (
            <button
              className="worker-strip"
              onClick={() => {
                setPanelOpen(true);
                setPanelTab("agents");
              }}
            >
              <LoaderCircle size={13} className="spin" />
              {runningWorkers} 个子代理执行或排队中
              <ChevronRight size={13} />
            </button>
          )}
          {!!runningCommands && !active?.busy && <button className="worker-strip" onClick={() => invoke("stop").catch(onError)}><Square size={13} />{runningCommands} 个后台命令正在运行 · 停止所有后台任务</button>}
          {!!active?.pendingInputs?.length && <div className="pending-inputs" aria-label="等待送达的补充要求">{active.pendingInputs.map(item=><div key={item.id}><span><small>{active.busy ? '等待当前步骤结束后送达' : '已保存，继续原任务后送达'}</small>{item.text}</span><button aria-label="取消补充要求" title="取消尚未送达的消息" onClick={()=>void window.xuloom.invoke('prompt.withdraw',{session:active.id,requestId:item.id}).catch(onError)}><X size={13}/></button></div>)}</div>}
          {active?.goal && <GoalStrip key={active.id} goal={active.goal} busy={active.busy} invoke={invoke} onError={onError} details={() => setGoalDialog("goal")} />}
          {active?.controlledExecution && <p className="fine-print" role="status">此子代理由原主会话控制。可以查看历史和文件，或从消息创建独立分支；继续执行请返回主会话。</p>}
          {active?.goalMode && <details className="goal-limits"><summary>可选运行上限 · 留空则持续运行</summary>
            <div>{([
              ["seconds", "累计执行秒数", 604800], ["output_tokens", "累计输出 tokens", 10000000], ["rounds", "最多执行轮数", 10000],
            ] as const).map(([key, label, max]) => <label key={key}>{label}<input aria-label={label} type="number" min={1} max={max} step={1} disabled={active.busy}
              value={active.goalLimits?.[key] ?? ""} placeholder="不限" onChange={e => {
                const limits = { ...active.goalLimits }; if (e.target.value === "") delete limits[key]; else limits[key] = Number(e.target.value);
                update({ goalLimits: limits });
              }} /></label>)}</div><p className="fine-print">主任务、子代理及摘要共用输出预算；这是运行限制，不是供应商账单金额保证。</p>
          </details>}
          <div className={"composer " + (!active ? "disabled" : "")} onPaste={(event) => { if (event.clipboardData.files.length) { event.preventDefault(); void importFiles(Array.from(event.clipboardData.files)); } }}>
            {active?.editingId && <div className="editing-prompt"><span>编辑上一条提示词 · 发送后重新执行，已修改的文件保留</span>
              <button aria-label="取消编辑提示词" disabled={active.busy} onClick={() => update({ editingId: undefined, draft: active.draftBeforeEdit || "", draftBeforeEdit: undefined, attachments: active.attachmentsBeforeEdit || [], attachmentsBeforeEdit: undefined })}><X size={13} /></button></div>}
            {active && <Attachments items={active.attachments} open={openAttachment} remote={!!active.remote} disabled={importing || active.busy} remove={(id) => update({ attachments: active.attachments.filter((item) => item.id !== id) })} />}
            {importing && <div className="attachment-loading" role="status"><LoaderCircle size={13} className="spin" />正在添加附件…{importOwner.current?.remote && <button className="text-button" onClick={() => { const owner = importOwner.current; if (owner) void window.xuloom.invoke("stop", { session: owner.id }).catch(onError); }}>取消附件传输</button>}</div>}
            <div className={"composer-input " + (active?.selected.length ? "has-skills" : "")}>
            {active && active.selected.length > 0 && (
              <div className="selected-skills">
                {active.selected.map((name) => (
                  <span className="selected-skill" key={name} title={"已选择技能：" + name + " · 在输入开头按 Backspace 移除"}>
                    <Box size={15} />
                    {name}
                  </span>
                ))}
              </div>
            )}
            <SkillInput key={activeId} sessionId={activeId} draft={active?.draft || ""}
              selected={active?.selected || []} capabilities={active?.capabilities}
              placeholder={active?.controlledExecution ? "子代理历史 · 请返回主会话继续执行" : active?.busy && active.steering ? "补充要求，将在当前步骤结束后送达…" : undefined}
              allowSkills={!active?.busy} disabled={opening || !active || (active.busy && !active.steering) || !!active.controlledExecution} inputRef={input} update={update} send={send} editPrevious={editLastPrompt} />
            </div>
            <div className="composer-toolbar">
              <div className="composer-options">
                <input ref={fileInput} type="file" multiple hidden aria-label="选择附件文件" onChange={(event) => { const files = Array.from(event.target.files || []); event.target.value = ""; void importFiles(files); }} />
                <ComposerMenu sessionId={activeId} disabled={opening || importing || !active || active.busy || !!active.controlledExecution} plan={active?.mode === "plan"} action={composerAction} />
                <select className={"permission-picker " + (active?.permissionMode === "full" ? "full-access" : "")} aria-label="访问权限"
                  title={active?.permissionMode === "full" ? "默认自动执行；显式权限规则、Plan 限制和中断恢复确认仍然生效" : "文件修改、命令和需要审批的外部工具会请求批准"}
                  disabled={opening || !active || permissionChanging || !!active.controlledExecution} value={active?.permissionMode || "ask"}
                  onChange={(event) => void setPermissionMode(event.target.value as PermissionMode)}>
                  <option value="ask">请求批准</option><option value="full">完全访问</option>
                </select>
                {active?.mode === "plan" && <button className="composer-mode-chip" aria-label="退出计划模式" title="计划模式 · 点击退出" onClick={() => update({ mode: "normal" })} disabled={active.busy}><Lightbulb size={14} />计划<X size={11} /></button>}
                {active?.goalMode && <button className="composer-goal-mode" aria-label="取消目标模式" title="目标模式：输入目标并发送 · 点击取消" disabled={active.busy} onClick={() => update({ goalMode: false })}><Target size={15} className="goal-mode-icon" /><X size={15} className="goal-mode-remove" /><span>目标</span></button>}
              </div>
              <div className="send-options">
                <select className="collaboration-picker" aria-label="协作档位"
                  title="Solo 独立完成；Auto 按需委派；Ultra 更积极寻找协作机会。两者不限制子代理数量，与模型思考强度、权限及宿主运行配置独立。"
                  disabled={opening || !active || collaborationChanging || active.busy || !!runningWorkers || !!runningCommands || !!active.controlledExecution}
                  value={active?.collaborationMode || "auto"}
                  onChange={(event) => void setCollaborationMode(event.target.value as SessionView["collaborationMode"])}>
                  <option value="solo">Solo · 独立</option><option value="auto">Auto · 自动</option><option value="ultra">Ultra · 增强</option>
                </select>
                {active?.controlledExecution ? <span className="model-button">{active.controlledModel || "子代理历史"}</span> : selectedModel?.connections?.some((connection) => connection.models.length) ? <select
                  className="model-picker" aria-label="选择模型" title={`${selectedModel.connectionName || "API"} · ${selectedModel.model}`}
                  disabled={opening || modelChanging || active?.busy || !!runningWorkers || !!runningCommands}
                  value={JSON.stringify([selectedModel.connectionId, selectedModel.model])}
                  onChange={(event) => void selectModel(event.target.value)}>
                  {selectedModel.connections.filter((connection) => connection.models.length).map((connection) =>
                    <optgroup key={connection.connectionId} label={connection.connectionName}>
                      {connection.models.map((model) => <option key={model} value={JSON.stringify([connection.connectionId, model])}>{model}</option>)}
                    </optgroup>)}
                </select> : <button className="model-button" onClick={openSettings}>连接模型<ChevronRight size={12} /></button>}
                {!active?.controlledExecution && <ReasoningControls profile={selectedModel?.modelReasoning?.[selectedModel.model]}
                  disabled={opening || modelChanging || !!active?.busy || !!runningWorkers || !!runningCommands}
                  change={value => void selectReasoning(value)} />}
                {active?.loaded.length ? (
                  <span
                    className="loaded-indicator"
                    title={active.loaded.join(", ")}
                  >
                    <Check size={12} />
                    {active.loaded.length} Skills
                  </span>
                ) : null}
                {active?.busy && active.steering && !!active.draft.trim() && <button className="send-button" aria-label="追加指令" title="保存并在下一个安全边界送达" disabled={steeringSending} onClick={() => void appendInput()}><ArrowUp size={19}/></button>}
                {active?.busy ? (
                  <button
                    className="send-button stop"
                    aria-label="停止任务"
                    onClick={() => invoke("stop").catch(onError)}
                  >
                    <Square size={15} fill="currentColor" />
                  </button>
                ) : (
                  <button
                    className={"send-button" + (resumeAvailable ? " resume" : "")}
                    aria-label={resumeAvailable ? "继续执行" : "发送任务"}
                    title={resumeAvailable ? "继续原任务，不新增消息" : "发送任务"}
                    disabled={opening || importing || modelChanging || permissionChanging || collaborationChanging || !!active?.controlledExecution || !!active?.attachments.some(item => item.available === false) || (!resumeAvailable && !active?.draft.trim() && !active?.attachments.length)}
                    onClick={() => void send()}
                  >
                    {resumeAvailable ? <Play size={16} fill="currentColor" /> : <ArrowUp size={19} />}
                  </button>
                )}
              </div>
            </div>
          </div>
          <div className="composer-caption">
            <span>
              <ShieldCheck size={12} />
              {active?.executionMode === "workspace" ? "系统隔离 · 工具进程仅可写工作目录，禁止网络" : active?.permissionMode === "full" ? "宿主执行 · 完全访问：遵守显式权限规则" : "宿主执行 · 请求批准：需要审批的工具会先确认"}
            </span>
            <span>
              <span className="capability-count">{enabledSkills.length} Skills 可用 ·{" "}
              {active?.capabilities.agents.filter(
                (agent) => agent.valid && !agent.shadowed,
              ).length || 0}{" "}
              代理角色</span>
              <span className="shortcut-hint">
                Enter 发送 · Shift Enter 换行
              </span>
            </span>
          </div>
        </div>
      </main>
      {navigationRepair && <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) setNavigationRepair(false); }}>
        <section className="dialog" role="dialog" aria-modal="true" aria-label="恢复 SSH 导航" onKeyDown={event => {
          if (event.key === "Escape") { event.stopPropagation(); setNavigationRepair(false); }
          if (event.key === "Tab") {
            const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled)')];
            if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
            else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
          }
        }}>
          <header><h2>恢复 SSH 导航</h2><button autoFocus aria-label="关闭导航恢复" onClick={() => setNavigationRepair(false)}><X size={17} /></button></header>
          <RemoteReferences action={workspaceAction} />
        </section>
      </div>}
      {executionDialog && active && <ExecutionDialog key={active.id} invoke={invoke} close={() => setExecutionDialog(false)} saved={(mode) => update({ executionMode: mode })} onError={onError} />}
      {createProjectOpen && <CreateProjectDialog create={(name, path) => workspaceAction("project.create", { name, path })} close={() => setCreateProjectOpen(false)} />}
      {contextOpen && active && <ContextDialog key={activeId} invoke={invoke} busy={active.busy || !!runningWorkers || !!runningCommands} close={() => setContextOpen(false)} />}
      {goalDialog && active && <div className="overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setGoalDialog(null); }}>
        <section className="dialog composer-goal-dialog" role="dialog" aria-modal="true" aria-label={goalDialog === "goal" ? "调整目标" : "定时唤醒"}
          onKeyDown={(event) => {
            if (event.key === "Escape") { event.stopPropagation(); setGoalDialog(null); }
            if (event.key === "Tab") {
              const items = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')];
              if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1)?.focus(); }
              else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0]?.focus(); }
            }
          }}>
          <header><strong>{goalDialog === "goal" ? "调整目标" : "定时唤醒"}</strong><button aria-label="关闭目标设置" onClick={() => setGoalDialog(null)} autoFocus><X size={17} /></button></header>
          {goalDialog === "goal" && active.goal ? <GoalEditor key={activeId} objective={active.goal.objective} invoke={invoke} close={() => setGoalDialog(null)} /> : <SchedulePanel key={activeId} invoke={invoke} onError={onError} />}
        </section>
      </div>}
      {panelOpen && (
        <div className="inspector-shell">
        <PanelResizer width={clampPanelWidth(filePreview ? filePanelWidth : panelWidth, viewport, effectiveSidebarWidth)} viewport={viewport} sidebarWidth={effectiveSidebarWidth} change={filePreview ? setFilePanelWidth : setPanelWidth}
          save={(width) => { void window.xuloom.invoke("layout.save", { [filePreview ? "filePreviewWidth" : "inspectorWidth"]: width }).catch(onError); }} />
        {filePreview && active && filePreview.ownerId === active.id ? <FilePreview sessionId={activeId} project={active.project}
          treeWidth={reviewTreeWidth} setTreeWidth={setReviewTreeWidth}
          saveTreeWidth={(value) => { void window.xuloom.invoke("layout.save", { reviewTreeWidth: value }).catch(onError); }}
          version={active.fileChanges?.find((file) => file.path === (filePreview.change?.path || filePreview.path) || relativeFile(file.path, active.project) === filePreview.path)?.updatedAt}
          {...filePreview} changes={active.fileChanges || []} close={() => setFilePreview(undefined)} onOpen={openFile} /> : <CapabilityPanel
            onOpenFile={openFile}
          key={activeId || "empty"}
          session={active}
          activityQuery={activityQuery}
          tab={panelTab}
          setTab={setPanelTab}
          invoke={invoke}
          refresh={refresh}
          update={update}
          onError={onError}
          onNavigateChat={(url) => navigateChat.current(url)}
        />}
        </div>
      )}
      {settingsOpen && (
        <SettingsDialog
          key={activeId}
          config={selectedModel || active?.config}
          targetLabel={active?.remote ? "SSH · " + active.remote.host : undefined}
          catalog={catalog}
          workspaceAction={workspaceAction}
          mediaControl={async (action, jobId) => { setSettingsOpen(false); try { await invoke("media.control", { action, jobId }); } catch (error) { onError(error); } }}
          defaultDirectory={defaultDirectory}
          chooseDirectory={() => window.xuloom.invoke("workspace.choose")}
          saveDirectory={async (directory) => {
            const value = await window.xuloom.invoke("workspace.save", { defaultDirectory: directory });
            setDefaultDirectory(value.defaultDirectory);
            return value.defaultDirectory;
          }}
          close={() => setSettingsOpen(false)}
          save={saveModel}
          openMcp={() => { setSettingsOpen(false); setMcpOpen(true); }}
          extensions={active ? { busy: active.busy || !!runningWorkers || !!runningCommands, invoke, changed: (capabilities) => update({ capabilities }) } : undefined}
          deleteConnection={async (connectionId) => {
            const config = await invoke("config.delete", { connectionId, confirm: true });
            applyModel(config);
            return config;
          }}
          discoverModels={(value) => invoke("config.models", value)}
        />
      )}
      {attachmentPreview && <AttachmentPreview image={attachmentPreview} close={() => setAttachmentPreview(undefined)} />}
      {scalingOpen && <ScalingWorkbench close={() => setScalingOpen(false)} />}
      {mcpOpen && (
        <McpDialog
          key={active?.id || "browse"}
          sessionId={active?.id || ""}
          busy={!!active?.busy || !!runningCommands || !!runningWorkers}
          creatingSession={opening}
          createSession={() => void openSession()}
          invoke={invoke}
          close={() => setMcpOpen(false)}
        />
      )}
    </div>
    </ReferenceNavigation.Provider>
  );
}
