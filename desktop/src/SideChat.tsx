import { useEffect, useRef } from "react";
import { ArrowUp, Square, X } from "lucide-react";
import type { SessionView } from "./types";
import Markdown from "./Markdown";
import TurnProcess from "./TurnProcess";
import GeneratedImages from "./GeneratedImages";
import { InteractionCard } from "./Dialogs";
import { t, tServer } from "./i18n";

export default function SideChat({ session, pending, close, update, send, stop, onError }: {
  session?: SessionView; pending: boolean; close: () => void; update: (draft: string) => void;
  send: () => void; stop: () => void; onError: (error: unknown) => void;
}) {
  const input = useRef<HTMLTextAreaElement>(null), bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { input.current?.focus(); }, [session?.id]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "nearest" }); }, [session?.messages]);
  return <section className="side-chat" aria-label={t("侧边聊天")} aria-busy={pending}>
    <header><div><strong>{t("侧边聊天")}</strong><small>{session?.config.model}</small></div>
      <button className="icon-button" aria-label={t("关闭侧边聊天")} onClick={close}><X size={18} /></button></header>
    <p className="side-chat-hint">{t("独立提问，不打断主任务。操作仍需批准；关闭面板不会停止侧聊。")}</p>
    <div className="side-chat-messages" aria-live="polite">
      {pending && <p role="status">{t("正在打开侧聊…")}</p>}
      {session?.messages.map((message, index) => <article key={index} className={"side-message " + message.role}>
        <small>{message.role === "user" ? t("你") : "Xuloom"}</small>
        {message.trace && <TurnProcess trace={message.trace} sessionId={session.id} />}
        <div className="markdown" data-quote-owner={session.id} data-quote-source={t("侧聊 · 消息 {number}", { number: index + 1 })}>
          {message.role === "user" ? <div className="user-prompt-text">{message.text}</div> : <Markdown text={message.text} sessionId={session.id} />}
        </div>
        <GeneratedImages files={message.trace?.files} sessionId={session.id} />
      </article>)}
      {session?.interactions.map(interaction => <InteractionCard key={interaction.requestId} interaction={interaction}
        respond={(requestId, answer) => window.xuloom.invoke("respond", { session: session.id, requestId, answer }).catch(onError)} />)}
      <div ref={bottom} />
    </div>
    {session?.error && <p className="error" role="alert">{tServer(session.error)}</p>}
    {session && <form onSubmit={event => { event.preventDefault(); send(); }}>
      <textarea ref={input} aria-label={t("侧聊输入")} placeholder={t("围绕选中内容提问…")} value={session.draft}
        disabled={pending || session.busy} onChange={event => update(event.target.value)}
        onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} />
      <small>{t("Enter 发送 · Shift+Enter 换行")}</small>
      {session.busy ? <button type="button" aria-label={t("停止侧聊")} onClick={stop}><Square size={16} />{t("停止侧聊")}</button> :
        <button type="submit" aria-label={t("发送侧聊")} disabled={!session.draft.trim() || pending}><ArrowUp size={16} />{t("发送侧聊")}</button>}
    </form>}
  </section>;
}
