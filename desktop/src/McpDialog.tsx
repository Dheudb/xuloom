import { useEffect, useRef, useState } from "react";
import { useDialogFocus } from './useDialogFocus';
import {
  X,
  Search,
  Plus,
  Plug,
  Globe,
  Terminal,
  LoaderCircle,
  RefreshCw,
  Check,
  ExternalLink,
} from "lucide-react";
import { t, tServer, tx } from "./i18n";

type Config = {
  type: string;
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
  enabled?: boolean;
  label?: string;
  catalogId?: string;
  savedUrlQuery?: boolean;
  auth?: { type: "oauth"; client_metadata_url?: string; client_id?: string; issuer?: string; redirect_uri?: string; client_secret?: string; token_endpoint_auth_method?: string };
  oauth?: { status: string };
};
type Entry = {
  id: string;
  name: string;
  description: string;
  source: string;
  homepage: string;
  requirements: string;
  config: Config;
  inputs: {
    name: string;
    description: string;
    optional?: boolean;
    secret?: boolean;
    defaultValue?: string;
  }[];
};
type Server = Config & {
  name: string;
  error?: string;
  status: string;
  connection: { status?: string; error?: string; tools?: string[] };
};
type Snapshot = {
  servers: Server[];
  configurationIssues?: { skill: string; path: string; name?: string; error: string }[];
  runtime: {
    available: boolean;
    python: string;
    executable: string;
    message: string;
  };
  needsReload: boolean;
  project: string;
};
type Editor = {
  entry?: Entry;
  server?: Server;
  name: string;
  type: string;
  command: string;
  args: string;
  url: string;
  pairs: { key: string; value: string }[];
  values: Record<string, string>;
  oauth: boolean;
  clientMetadataUrl: string;
  registration: string;
  clientId: string;
  issuer: string;
  redirectUri: string;
  clientSecret: string;
  clearClientSecret: boolean;
  clientAuth: string;
};
const statusText: Record<string, string> = {
  connected: t("已连接"),
  disabled: t("已停用"),
  pending: t("下次任务连接"),
  error: t("连接失败"),
  invalid: t("配置需修复"),
};

export default function McpDialog({
  sessionId,
  busy,
  creatingSession,
  createSession,
  invoke,
  close,
}: {
  sessionId: string;
  busy: boolean;
  creatingSession: boolean;
  createSession: () => void;
  invoke: (method: string, params?: Record<string, unknown>) => Promise<any>;
  close: () => void;
}) {
  const [tab, setTab] = useState("market");
  const [source, setSource] = useState("builtin");
  const [query, setQuery] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cursor, setCursor] = useState("");
  const [skipped, setSkipped] = useState(0);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [tests, setTests] = useState<
    Record<string, { status: string; error?: string; tools?: string[] }>
  >({});
  const [removing, setRemoving] = useState("");
  const [authorization, setAuthorization] = useState<{name: string; url: string; callback: string} | null>(null);
  const [callbackUrl, setCallbackUrl] = useState("");
  const signingIn = useRef("");
  const dialog=useDialogFocus();
  const locked = !sessionId || busy || !!working;

  useEffect(() => {
    let alive = true;
    if (sessionId) invoke("mcp.list")
      .then((value) => alive && setSnapshot(value))
      .catch((e) => alive && setError(String(e)));
    load("builtin", "");
    const unsubscribe = window.xuloom.onEvent((event) => {
      if (sessionId && event.session === sessionId && event.type === "mcp_changed")
        setSnapshot(event.snapshot);
      if (sessionId && event.session === sessionId && event.type === "mcp_oauth_authorize")
        setAuthorization({ name: event.name, url: event.url, callback: event.callback });
    });
    return () => {
      alive = false;
      unsubscribe();
      if (signingIn.current) void invoke("mcp.oauth.cancel", { name: signingIn.current }).catch(() => {});
    };
  }, [sessionId]);

  async function load(nextSource = source, nextQuery = query, nextCursor = "") {
    setLoading(true);
    setError("");
    try {
      const result = await invoke("mcp.market", {
        source: nextSource,
        query: nextQuery,
        cursor: nextCursor,
      });
      setEntries((previous) =>
        nextCursor
          ? [
              ...previous,
              ...result.entries.filter(
                (item: Entry) => !previous.some((old) => old.id === item.id),
              ),
            ]
          : result.entries,
      );
      setCursor(result.cursor || "");
      setSkipped(result.skipped || 0);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  function edit(entry?: Entry, server?: Server) {
    const config = server || entry?.config;
    setRemoving("");
    setError("");
    setMessage("");
    setEditor({
      entry,
      server,
      name: server?.name || entry?.id || "",
      type: config?.type || "http",
      command: config?.command || "",
      args: JSON.stringify(config?.args || []),
      url: config?.url || "",
      oauth: config?.auth?.type === "oauth",
      clientMetadataUrl: config?.auth?.client_metadata_url || "",
      registration: config?.auth?.client_id ? "fixed" : "automatic",
      clientId: config?.auth?.client_id || "",
      issuer: config?.auth?.issuer || "",
      redirectUri: config?.auth?.redirect_uri || "",
      clientSecret: "",
      clearClientSecret: false,
      clientAuth: config?.auth?.token_endpoint_auth_method || "auto",
      pairs: Object.entries(
        config?.type === "stdio" ? config.env || {} : config?.headers || {},
      ).map(([key, value]) => ({ key, value })),
      values: Object.fromEntries(
        (entry?.inputs || []).map((spec) => [
          spec.name,
          spec.defaultValue || "",
        ]),
      ),
    });
  }

  async function action(
    method: string,
    params: Record<string, unknown>,
    label: string,
  ) {
    setWorking(label);
    setError("");
    setMessage("");
    try {
      const result = await invoke(method, params);
      if (method === "mcp.test") {
        setTests((old) => ({ ...old, [String(params.name)]: result }));
        setMessage(
          result.status === "connected"
            ? t("连接成功，发现 {length} 个工具。", { length: result.tools.length })
            : result.error || result.runtime.message,
        );
      } else {
        setSnapshot(result);
        setTests({});
        if (method === "mcp.install" || method === "mcp.save") {
          setEditor(null);
          setTab("installed");
          setMessage(t("配置已保存。启用后将在下次任务连接；也可以先测试连接。"));
        }
      }
      setRemoving("");
    } catch (e) {
      setError(String(e));
    } finally {
      setWorking("");
    }
  }

  async function save() {
    if (!editor) return;
    if (editor.entry)
      return action(
        "mcp.install",
        { id: editor.entry.id, name: editor.name, values: editor.values },
        "save",
      );
    try {
      const mapping = Object.fromEntries(
        editor.pairs
          .filter((p) => p.key.trim())
          .map((p) => [p.key.trim(), p.value]),
      );
      const config: Config = {
        type: editor.type,
        enabled: editor.server?.enabled ?? false,
        label: editor.server?.label,
        catalogId: editor.server?.catalogId,
      };
      if (editor.type === "stdio")
        Object.assign(config, {
          command: editor.command,
          args: JSON.parse(editor.args),
          env: mapping,
        });
      else
        Object.assign(config, {
          url: editor.url,
          headers: mapping,
          savedUrlQuery: editor.server?.savedUrlQuery,
        });
      if (editor.type === "http" && editor.oauth) {
        config.auth = editor.registration === "fixed" ? { type: "oauth", client_id: editor.clientId.trim(), issuer: editor.issuer.trim(), redirect_uri: editor.redirectUri.trim(), token_endpoint_auth_method: editor.clientAuth,
          ...(editor.clientSecret ? { client_secret: editor.clientSecret } : !editor.clearClientSecret && editor.server?.auth?.client_secret !== undefined ? { client_secret: "" } : {})
        } : { type: "oauth", ...(editor.clientMetadataUrl.trim() ? { client_metadata_url: editor.clientMetadataUrl.trim() } : {}) };
      }
      await action(
        "mcp.save",
        { name: editor.name, config, existing: !!editor.server },
        "save",
      );
    } catch {
      setError(t("启动参数必须是 JSON 字符串数组，例如 [\"-y\", \"包名\"]。"));
    }
  }

  async function authorize(name: string, signOut = false) {
    setWorking((signOut ? "oauth-logout:" : "oauth:") + name); setError(""); setMessage("");
    if (!signOut) signingIn.current = name;
    try {
      const result = await invoke(signOut ? "mcp.oauth.logout" : "mcp.oauth.login", { name });
      setSnapshot(result.snapshot);
      setTests(old => { const next = { ...old }; delete next[name]; return next; });
      setMessage(result.status === "authorized" ? t("授权已保存，下次连接会使用此账号。") : result.status === "cancelled" ? t("已取消等待，账号状态以列表为准；如需清除授权，请退出登录。") : result.revocation === "failed" ? t("已清除本地授权；服务端撤销失败，可到服务的账号页面撤销。") : t("已清除本地授权。服务端是否撤销以该服务的支持范围为准。"));
    } catch (e) { setError(String(e)); }
    finally { signingIn.current = ""; setAuthorization(null); setCallbackUrl(""); setWorking(""); }
  }

  return (
    <div className="overlay" onClick={() => !working && close()}>
      <section
        ref={dialog}
        onKeyDown={e=>{if(e.key==='Escape' && !working){e.stopPropagation();close();}}}
        role="dialog"
        aria-modal="true"
        aria-label={t("MCP 市场")}
        className={"dialog mcp-dialog " + (editor ? "with-editor" : "")}
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <div>
            <span className="eyebrow">TOOLS & CONNECTIONS</span>
            <h2>{t("MCP 市场")}</h2>
            <p className="mcp-subtitle">{sessionId ? t("为当前工作区添加搜索、文档和开发工具") : t("浏览搜索、文档和开发工具")}</p>
          </div>
          <button
            className="icon-button"
            aria-label={t("关闭 MCP 市场")}
            disabled={!!working}
            onClick={close}
          >
            <X size={18} />
          </button>
        </header>
        {working.startsWith("oauth:") && <section className="form-section" aria-label={t("MCP OAuth 登录")}>
          <p role="status">{authorization ? t("请在服务的授权页面确认账号及权限。") : t("正在连接授权服务…")}</p>
          {authorization && <><button onClick={() => void window.xuloom.invoke("external.open", { url: authorization.url }).catch(e => setError(String(e)))}>{t("打开授权页面")}</button>
            <details><summary>{t("SSH / 手动完成回调")}</summary><p className="mcp-footnote">{t("远端登录时，可将浏览器地址栏的完整回调地址粘贴到这里。授权码不会进入聊天或模型上下文。")}</p>
              <label>{t("完整回调地址")}<input aria-label={t("OAuth 回调地址")} type="password" autoComplete="off" value={callbackUrl} onChange={e => setCallbackUrl(e.target.value)} /></label>
              <button disabled={!callbackUrl} onClick={() => void invoke("mcp.oauth.callback", { name: authorization.name, url: callbackUrl }).then(() => setCallbackUrl("")).catch(e => setError(String(e)))}>{t("提交授权回调")}</button>
            </details></>}
          {signingIn.current && <button onClick={() => void invoke("mcp.oauth.cancel", { name: signingIn.current }).catch(e => setError(String(e)))}>{t("取消登录")}</button>}
        </section>}
        <div className="mcp-toolbar">
          <div className="mcp-tabs">
            <button
              className={tab === "market" ? "active" : ""}
              onClick={() => setTab("market")}
            >{t("浏览市场")}</button>
            <button
              className={tab === "installed" ? "active" : ""}
              disabled={!sessionId}
              title={!sessionId ? t("打开会话后管理对应工作区的服务") : undefined}
              onClick={() => setTab("installed")}
            >{tx("我的服务 · {value}", { value: snapshot?.servers.length || 0 })}
            </button>
          </div>
          <button disabled={locked} onClick={() => edit()}>
            <Plus size={14} />{t("自定义服务")}</button>
        </div>
        {!sessionId && <div className="notice" role="status">
          <p>{t("可以直接浏览和搜索市场。安装前请新建或打开一个会话，服务配置会保存在该会话的工作区，并与 CLI 共用。")}</p>
          <button disabled={creatingSession} onClick={createSession}>{creatingSession ? t("正在创建会话…") : t("新建会话并配置")}</button>
        </div>}
        {busy && (
          <p className="notice">{t("当前有任务正在运行，完成或停止后可以修改 MCP 配置。")}</p>
        )}
        {snapshot && !snapshot.runtime.available && (
          <p className="mcp-warning">
            {tx("{message} 当前 Python：{python}。市场浏览和保存配置仍可使用。", { message: tServer(snapshot.runtime.message), python: snapshot.runtime.python })}</p>
        )}
        {error && (
          <p role="alert" className="mcp-warning">
            {tServer(error.replace(/^Error: /, ""))}
          </p>
        )}
        {message && (
          <p role="status" className="mcp-message">
            {tServer(message)}
          </p>
        )}
        <div className="mcp-body">
          <div className="mcp-main">
            {tab === "market" ? (
              <>
                <form
                  className="mcp-search"
                  onSubmit={(e) => {
                    e.preventDefault();
                    load();
                  }}
                >
                  <select
                    aria-label={t("市场来源")}
                    value={source}
                    disabled={loading}
                    onChange={(e) => {
                      setSource(e.target.value);
                      setEntries([]);
                      setCursor("");
                      load(e.target.value, query);
                    }}
                  >
                    <option value="builtin">{t("精选目录")}</option>
                    <option value="registry">{t("官方 MCP 注册表")}</option>
                  </select>
                  <input
                    aria-label={t("搜索 MCP")}
                    placeholder={t("搜索服务名称或用途…")}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  <button disabled={loading}>
                    <Search size={15} />
                    {loading ? t("搜索中") : t("搜索")}
                  </button>
                </form>
                <div className="mcp-grid">
                  {entries.map((entry) => {
                    const installed = snapshot?.servers.some(
                      (s) => s.catalogId === entry.id || s.name === entry.id,
                    );
                    return (
                      <article className="mcp-card" key={entry.id}>
                        <div className="mcp-card-head">
                          {entry.config.type === "http" ? (
                            <Globe size={18} />
                          ) : (
                            <Terminal size={18} />
                          )}
                          <strong>{entry.name}</strong>
                          <span>{entry.source}</span>
                        </div>
                        <p>{entry.description}</p>
                        <small>{entry.requirements}</small>
                        <div className="mcp-card-actions">
                          {entry.homepage && (
                            <button
                              className="mcp-doc-link"
                              onClick={() =>
                                window.xuloom
                                  .invoke("external.open", {
                                    url: entry.homepage,
                                  })
                                  .catch((e) => setError(String(e)))
                              }
                            >
                              <ExternalLink size={12} />{t("文档")}</button>
                          )}
                          <button
                            disabled={locked || !!installed}
                            onClick={() => edit(entry)}
                          >
                            {installed ? (
                              <>
                                <Check size={13} />{t("已安装")}</>
                            ) : (
                              <>
                                <Plus size={13} />{t("安装")}</>
                            )}
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
                {!entries.length && !loading && (
                  <div className="instruction-empty">
                    <Search size={24} />
                    <p>{t("没有匹配的可安装服务。换个关键词或添加自定义服务。")}</p>
                  </div>
                )}
                {skipped > 0 && (
                  <p className="mcp-footnote">{tx("本页 {skipped} 条记录需要暂未支持的启动参数或连接方式，未列入可安装列表。", { skipped })}</p>
                )}
                {cursor && (
                  <button
                    disabled={loading}
                    onClick={() => load(source, query, cursor)}
                  >
                    {loading ? t("加载中…") : t("加载更多")}
                  </button>
                )}
              </>
            ) : (
              <>
                <div className="mcp-installed-heading">
                  <p>{t("配置保存在当前工作区，与 CLI 共用。")}</p>
                  <button
                    disabled={locked}
                    onClick={() => action("mcp.reload", {}, "reload")}
                  >
                    <RefreshCw size={13} />{t("刷新连接")}</button>
                </div>
                {snapshot?.needsReload && (
                  <p className="notice">{t("配置已改变，下一次任务会重新连接；也可点击刷新连接立即应用到当前会话。")}</p>
                )}
                {!!snapshot?.configurationIssues?.length && (
                  <div className="notice" role="status">
                    <strong>{t("部分 Skill 的 MCP 配置未加载，其他服务可继续使用。")}</strong>
                    {snapshot.configurationIssues.map((issue, index) => (
                      <p key={index}><span>{issue.skill}{issue.name ? ` / ${issue.name}` : ""}：{tServer(issue.error)}</span><br /><code>{issue.path}</code></p>
                    ))}
                  </div>
                )}
                {!snapshot?.servers.length && (
                  <div className="instruction-empty">
                    <Plug size={28} />
                    <h4>{t("还没有 MCP 服务")}</h4>
                    <p>{t("从市场安装搜索服务，或添加自己的连接。")}</p>
                  </div>
                )}
                {snapshot?.servers.map((server) => {
                  const test = tests[server.name];
                  return (
                    <article className="mcp-card installed" key={server.name}>
                      <div className="mcp-card-head">
                        <Plug size={17} />
                        <strong>{server.label || server.name}</strong>
                        <span className={"mcp-status " + server.status}>
                          {statusText[server.status] || server.status}
                        </span>
                      </div>
                      <code>
                        {server.type === "stdio"
                          ? [server.command, ...(server.args || [])].join(" ")
                          : server.url}
                      </code>
                      <p>
                        {server.error ||
                          server.connection?.error ||
                          test?.error ||
                          (test?.status === "connected"
                            ? t("测试成功：{value}", { value: test.tools?.join("、") || t("没有工具") })
                            : server.connection?.tools?.length
                              ? t("{length} 个工具可用", { length: server.connection.tools.length })
                              : server.name)}
                      </p>
                      <div className="mcp-card-actions">
                        {server.auth?.type === "oauth" && <><span className="mcp-footnote">{server.oauth?.status === "authorized" ? t("已授权") : server.oauth?.status === "unavailable" ? t("授权不可用") : t("未登录")}</span>
                          <button disabled={locked || !snapshot.runtime.available} onClick={() => void authorize(server.name)}>{server.oauth?.status === "authorized" ? t("重新登录") : t("登录")}</button>
                          <button disabled={locked || !snapshot.runtime.available} onClick={() => void authorize(server.name, true)}>{t("退出登录")}</button></>}
                        <button
                          disabled={locked}
                          onClick={() => edit(undefined, server)}
                        >{t("配置")}</button>
                        <button
                          disabled={locked || !snapshot.runtime.available}
                          onClick={() =>
                            action(
                              "mcp.test",
                              { name: server.name },
                              server.name,
                            )
                          }
                        >
                          {tx("{value}测试连接", { value: working === server.name ? (
                            <LoaderCircle className="spin" size={13} />
                          ) : (
                            <Plug size={13} />
                          ) })}</button>
                        <button
                          disabled={locked}
                          onClick={() =>
                            action(
                              "mcp.toggle",
                              {
                                name: server.name,
                                enabled: server.enabled === false,
                              },
                              "toggle",
                            )
                          }
                        >
                          {server.enabled === false ? t("启用") : t("停用")}
                        </button>
                        <button
                          className="danger"
                          disabled={locked}
                          onClick={() =>
                            removing === server.name
                              ? action(
                                  "mcp.remove",
                                  { name: server.name },
                                  "remove",
                                )
                              : setRemoving(server.name)
                          }
                        >
                          {removing === server.name ? t("确认移除") : t("移除")}
                        </button>
                      </div>
                    </article>
                  );
                })}
              </>
            )}
          </div>
          {editor && (
            <aside className="mcp-editor">
              <div className="mcp-editor-title">
                <h3>
                  {editor.entry
                    ? t("安装 {name}", { name: editor.entry.name })
                    : editor.server
                      ? t("编辑服务")
                      : t("添加自定义 MCP")}
                </h3>
                <button
                  className="icon-button"
                  aria-label={t("取消 MCP 配置")}
                  disabled={!!working}
                  onClick={() => setEditor(null)}
                >
                  <X size={15} />
                </button>
              </div>
              <div className="form-section">
                <label>{t("服务标识")}<input
                    aria-label={t("MCP 服务标识")}
                    value={editor.name}
                    disabled={!!editor.server || locked}
                    placeholder="my-search"
                    onChange={(e) =>
                      setEditor({ ...editor, name: e.target.value })
                    }
                  />
                </label>
                {editor.entry ? (
                  <>
                    <p className="mcp-footnote">{editor.entry.requirements}</p>
                    <pre>
                      {editor.entry.config.type === "http"
                        ? editor.entry.config.url
                        : [
                            editor.entry.config.command,
                            ...(editor.entry.config.args || []),
                          ].join(" ")}
                    </pre>
                    {editor.entry.inputs.map((spec) => (
                      <label key={spec.name}>
                        {spec.name}
                        {spec.optional ? t("（可选）") : " *"}
                        <input
                          type={spec.secret ? "password" : "text"}
                          autoComplete="off"
                          aria-label={spec.name}
                          value={editor.values[spec.name] || ""}
                          onChange={(e) =>
                            setEditor({
                              ...editor,
                              values: {
                                ...editor.values,
                                [spec.name]: e.target.value,
                              },
                            })
                          }
                        />
                        <small>{spec.description}</small>
                      </label>
                    ))}
                  </>
                ) : (
                  <>
                    <label>{t("连接方式")}<select
                        aria-label={t("MCP 连接方式")}
                        value={editor.type}
                        disabled={locked}
                        onChange={(e) =>
                          setEditor({
                            ...editor,
                            type: e.target.value,
                            pairs: [],
                          })
                        }
                      >
                        <option value="http">{t("远程 HTTP（Streamable HTTP）")}</option>
                        <option value="stdio">{t("本地程序（stdio）")}</option>
                      </select>
                    </label>
                    {editor.type === "http" ? (
                      <><label>{t("服务地址")}<input
                          aria-label={t("MCP 服务地址")}
                          placeholder="https://example.com/mcp"
                          value={editor.url}
                          onChange={(e) =>
                            setEditor({ ...editor, url: e.target.value })
                          }
                        />
                      </label><label>{t("认证方式")}<select aria-label={t("MCP 认证方式")} value={editor.oauth ? "oauth" : "headers"} onChange={e => setEditor({ ...editor, oauth: e.target.value === "oauth" })}><option value="headers">{t("无需登录 / 手动请求头")}</option><option value="oauth">{t("OAuth · 浏览器登录")}</option></select></label>
                        {editor.oauth && <><p className="mcp-footnote">{t("保存后点击“登录”。授权令牌仅保存在系统保护的本机存储中，不发送给聊天模型。不能同时填写 Authorization 请求头。")}</p>
                          <label>{t("客户端注册")}<select aria-label={t("OAuth 客户端注册")} value={editor.registration} onChange={e => setEditor({ ...editor, registration: e.target.value })}><option value="automatic">{t("自动注册 / 客户端元数据文档")}</option><option value="fixed">{t("使用已有 Client ID")}</option></select></label>
                          {editor.registration === "fixed" ? <>
                            <label>Client ID<input aria-label="OAuth Client ID" value={editor.clientId} onChange={e => setEditor({ ...editor, clientId: e.target.value })} /></label>
                            <label>{t("授权服务器 Issuer")}<input aria-label="OAuth Issuer" placeholder={t("服务公布的授权服务器标识")} value={editor.issuer} onChange={e => setEditor({ ...editor, issuer: e.target.value })} /></label>
                            <label>{t("已注册的回调地址")}<input aria-label={t("OAuth 注册回调地址")} placeholder={t("http://127.0.0.1:端口/回调路径")} value={editor.redirectUri} onChange={e => setEditor({ ...editor, redirectUri: e.target.value })} /></label>
                            <label>{t("Client Secret（可选）")}<input aria-label="OAuth Client Secret" type="password" autoComplete="new-password" placeholder={!editor.clearClientSecret && editor.server?.auth?.client_secret !== undefined ? t("已保存；留空保留") : t("仅在服务方提供时填写")} value={editor.clientSecret} onChange={e => setEditor({ ...editor, clientSecret: e.target.value, clearClientSecret: false, clientAuth: e.target.value && editor.clientAuth === "none" ? "auto" : editor.clientAuth })} /></label>
                            {editor.server?.auth?.client_secret !== undefined && <button type="button" onClick={() => setEditor({ ...editor, clearClientSecret: !editor.clearClientSecret, clientSecret: "", clientAuth: "auto" })}>{editor.clearClientSecret ? t("保留已保存 Secret") : t("清除已保存 Secret")}</button>}
                            <details><summary>{t("客户端认证（高级）")}</summary><label>{t("Token 端点认证")}<select aria-label={t("OAuth Token 认证")} value={editor.clientAuth} onChange={e => setEditor({ ...editor, clientAuth: e.target.value })}><option value="auto">{t("自动 · 使用服务公布的方式")}</option><option value="none">{t("公开客户端 · 无 Secret")}</option><option value="client_secret_basic">Client Secret Basic</option><option value="client_secret_post">Client Secret POST</option></select></label></details>
                            <p className="mcp-footnote">{t("Issuer 与回调地址应与服务方登记一致；回调只支持本机 127.0.0.1。端口占用时会停止登录，不会另选地址或重新注册。")}</p>
                          </> : <details><summary>{t("客户端元数据（可选）")}</summary><label>{t("HTTPS 客户端元数据地址")}<input aria-label={t("OAuth 客户端元数据地址")} value={editor.clientMetadataUrl} onChange={e => setEditor({ ...editor, clientMetadataUrl: e.target.value })} /></label></details>}
                        </>}
                      </>
                    ) : (
                      <>
                        <label>{t("启动程序")}<input
                            aria-label={t("MCP 启动程序")}
                            placeholder={t("npx / uvx / Python 路径")}
                            value={editor.command}
                            onChange={(e) =>
                              setEditor({ ...editor, command: e.target.value })
                            }
                          />
                        </label>
                        <label>{t("启动参数（JSON 数组）")}<textarea
                            aria-label={t("MCP 启动参数")}
                            value={editor.args}
                            onChange={(e) =>
                              setEditor({ ...editor, args: e.target.value })
                            }
                          />
                        </label>
                      </>
                    )}
                    <p className="mcp-footnote">
                      {tx("{value}。已保存的值留空即可保留；删除一行可清除该项。", { value: editor.type === "http" ? t("HTTP 请求头") : t("环境变量") })}</p>
                    {editor.pairs.map((pair, index) => (
                      <div className="mcp-pair" key={index}>
                        <input
                          aria-label={t("配置名称 {value}", { value: index + 1 })}
                          placeholder={
                            editor.type === "http" ? "Authorization" : "API_KEY"
                          }
                          value={pair.key}
                          onChange={(e) =>
                            setEditor({
                              ...editor,
                              pairs: editor.pairs.map((p, i) =>
                                i === index ? { ...p, key: e.target.value } : p,
                              ),
                            })
                          }
                        />
                        <input
                          type="password"
                          autoComplete="off"
                          aria-label={t("配置值 {value}", { value: index + 1 })}
                          placeholder={
                            editor.server ? t("已保存的值留空保留") : t("值")
                          }
                          value={pair.value}
                          onChange={(e) =>
                            setEditor({
                              ...editor,
                              pairs: editor.pairs.map((p, i) =>
                                i === index
                                  ? { ...p, value: e.target.value }
                                  : p,
                              ),
                            })
                          }
                        />
                        <button
                          className="icon-button"
                          aria-label={t("删除配置 {value}", { value: index + 1 })}
                          onClick={() =>
                            setEditor({
                              ...editor,
                              pairs: editor.pairs.filter((_, i) => i !== index),
                            })
                          }
                        >
                          <X size={12} />
                        </button>
                      </div>
                    ))}
                    <button
                      className="mcp-add-pair"
                      onClick={() =>
                        setEditor({
                          ...editor,
                          pairs: [...editor.pairs, { key: "", value: "" }],
                        })
                      }
                    >
                      <Plus size={12} />{t("添加字段")}</button>
                  </>
                )}
              </div>
              <p className="mcp-footnote">{t("保存配置不会启动服务。测试连接会启动本地程序或访问远程地址；启用后，工具调用遵循会话权限。")}</p>
              <button
                className="primary mcp-save"
                disabled={locked || !editor.name.trim()}
                onClick={save}
              >
                {working === "save"
                  ? t("保存中…")
                  : editor.entry
                    ? t("安装并保存配置")
                    : t("保存配置")}
              </button>
            </aside>
          )}
        </div>
        <footer className="mcp-footer">
          <span>{t("内置 DDGS 多搜索源搜索可独立使用。")}</span>
          <span>{sessionId ? t("当前工作区 · {value}", { value: snapshot?.project.split(/[\\/]/).pop() || t("正在读取…") }) : t("尚未选择安装工作区")}</span>
        </footer>
      </section>
    </div>
  );
}
