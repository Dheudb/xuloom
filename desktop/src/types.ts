export type Skill = {
  id: string;
  name: string;
  description: string;
  root: string;
  path: string;
  scope: string;
  enabled: boolean;
  valid: boolean;
  error: string;
  shadowed: boolean;
  compatibility: string;
};
export type AgentDefinition = {
  name: string;
  description?: string;
  when_to_use?: string;
  scope: string;
  valid: boolean;
  shadowed: boolean;
  error?: string;
  model?: string;
  allowed_tools?: string[];
  skills?: string[];
  path?: string;
};
export type Capabilities = {
  skills: Skill[];
  agents: AgentDefinition[];
  settings: {
    skill_roots?: string[];
    agent_roots?: string[];
    disabled_skills?: string[];
    projectSkillsRoot?: string;
    projectAgentsRoot?: string;
    globalSkillsRoot?: string;
    globalAgentsRoot?: string;
  };
};
export type ReasoningProfile = {
  adapter: "openai" | "deepseek" | "qwen" | "qwen-local" | "anthropic" | "anthropic-budget";
  efforts: string[];
  canDisable: boolean;
  thinking: "default" | "enabled" | "disabled";
  effort: string;
  budgetTokens: number;
};
export type ModelConfig = {
  connectionId?: string;
  connectionName?: string;
  provider: string;
  model: string;
  baseUrl: string;
  hasKey: boolean;
  networkMode?: "direct" | "system";
  models?: string[];
  modelInputs?: Record<string, ("text" | "image" | "document")[]>;
  modelReasoning?: Record<string, ReasoningProfile>;
  connections?: ModelConnection[];
};
export type ModelConnection = Omit<ModelConfig, "connections"> & { connectionId: string; connectionName: string; models: string[] };
export type TurnTrace = {
  modelStreams?: { turn: number; status: string; elapsedMs?: number; connectedMs?: number; firstChunkMs?: number; firstTextMs?: number; lastProgressMs?: number; textChars?: number; reasoningChunks?: number; finishReason?: string; error?: string }[];
  userMessageId?: string;
  files?: FileChange[];
  id: string;
  status: string;
  startedAt?: number;
  finishedAt?: number;
  elapsedMs?: number;
  resumedAt?: number;
  elapsedBeforeResumeMs?: number;
  approximate?: boolean;
  answer?: string;
  pendingText?: string;
  steps: ({ kind: "text"; text: string } | { kind: "tool"; tool: ToolActivity })[];
};
export type Attachment = { id: string; name: string; path: string; size: number; kind: "image" | "file"; preview?: string; available?: boolean; state?: string };
export type HistoryItem = { id?: string; role: "user" | "assistant"; text: string; trace?: TurnTrace; attachments?: Attachment[] };
export type SavedSession = { id: string; title: string; updated: number };
export type CatalogSession = SavedSession & {
  remote?: RemoteLocation;
  scope?: "project" | "standalone";
  permissionMode: PermissionMode;
  sessionId: string;
  project: string;
  created: number;
  pinned: boolean;
  archived: boolean;
  error?: string;
  draft?: string;
  draftAttachments?: string[];
};
export type CatalogProject = {
  remote?: RemoteLocation;
  path: string;
  name: string;
  open: boolean;
  pinned: boolean;
  archived: boolean;
  collapsed?: boolean;
  error?: string;
  sessions: CatalogSession[];
};
export type SessionCatalog = {
  navigationIssues?: number;
  standalone?: CatalogSession[];
  revision?: number;
  projects: CatalogProject[];
  view: { sort: "recent" | "oldest" | "name" | "created" };
};
export type InitialSession = {
  controlledExecution?: boolean;
  controlledModel?: string;
  remote?: RemoteLocation;
  executionMode?: "host" | "workspace";
  collaborationMode?: "solo" | "auto" | "ultra";
  fileChanges?: FileChange[];
  scope?: "project" | "standalone";
  permissionMode?: PermissionMode;
  id: string;
  persistedId?: string;
  project: string;
  title?: string;
  savedDraft?: string;
  savedDraftEditId?: string;
  savedAttachments?: Attachment[];
  config: ModelConfig;
  capabilities: Capabilities;
  sessions: SavedSession[];
  history: HistoryItem[];
  activity?: ToolActivity[];
  workers?: Worker[];
};
export type RemoteLocation = { connectionId: string; host: string; name: string; key: string; hostname?: string; attachments?: boolean };
export type EngineEvent = { session: string; type: string; [key: string]: any };
export type Worker = {
  backend?: "native" | "claude" | "pi";
  executor?: string;
  outcome_unknown?: boolean;
  external_recovery?: { resumable: boolean; reason: string } | null;
  pending_message_count?: number;
  remote_reference?: { url: string; host: string; cwd: string; machine: string; session_id: string; connection_id: string };
  agent_id: string;
  agent_type?: string;
  description?: string;
  status: string;
  task?: string;
  phase?: string;
  started_at?: number;
  finished_at?: number;
  result?: string;
  model?: string;
  background?: boolean;
};
export type ToolMedia = { id?: string; kind: "image"; name: string; width?: number; height?: number; error?: string };
export type ToolActivity = {
  id: string;
  name: string;
  status: string;
  result?: string;
  media?: ToolMedia[];
  input?: Record<string, any>;
  agentId?: string;
  agentRole?: string;
  startedAt?: number;
  finishedAt?: number;
  waitingRequestId?: string;
};
export type SessionView = InitialSession & {
  goal?: { objective: string; status: string; elapsed_seconds?: number; round_started_at?: number; phase?: string; budget?: { seconds: number } } | null;
  goalMode?: boolean;
  collaborationMode: "solo" | "auto" | "ultra";
  permissionMode: PermissionMode;
  persistedId?: string;
  busy: boolean;
  error: string;
  messages: HistoryItem[];
  streamIndex: number | null;
  tools: ToolActivity[];
  workers: Worker[];
  commands: Record<string, { id: string; status: string; owner: string }>;
  interactions: EngineEvent[];
  loaded: string[];
  logs: string[];
  selected: string[];
  mode: string;
  draft: string;
  editingId?: string;
  draftBeforeEdit?: string;
  attachments: Attachment[];
  attachmentsBeforeEdit?: Attachment[];
};
export type PermissionMode = "ask" | "full";
export type FileChange = {
  revisionId?: string;
  source?: "git";
  path: string; operation: string; updatedAt: number;
  added: number | null; removed: number | null; diff: string;
  statsAvailable: boolean; truncated?: boolean; legacy?: boolean;
};
declare global {
  interface Window {
    xuloom: {
      flushDrafts(drafts: { id: string; draft: string; draftEditId: string; draftAttachments: string[] }[]): boolean;
      importFiles(session: string, files: File[], existing: string[], ids?: string[]): Promise<Attachment[]>;
      invoke<T = any>(
        method: string,
        params?: Record<string, unknown>,
      ): Promise<T>;
      onEvent(callback: (event: EngineEvent) => void): () => void;
    };
  }
}
