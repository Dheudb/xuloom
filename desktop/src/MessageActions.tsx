import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, Copy, GitFork } from "lucide-react";
import { t } from "./i18n";

export default function MessageActions({ role, text, fork, disabled, onError, children }: {
  role: "user" | "assistant"; text: string; fork?: () => Promise<void>; disabled: boolean;
  onError: (error: unknown) => void; children?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    try {
      await window.xuloom.invoke("clipboard.writeText", { text });
      setCopied(true); clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1800);
    } catch (error) { onError(error); }
  }
  const label = role === "user" ? t("复制提示词") : t("复制回复");
  return <div className="message-actions" role="group" aria-label={role === "user" ? t("用户消息操作") : t("回复操作")}>
    <button type="button" aria-label={label} title={copied ? t("已复制") : label} disabled={!text} onClick={() => void copy()}>
      {copied ? <Check size={14} /> : <Copy size={14} />}
      {role === "assistant" && <span className="message-copy-label">{t("复制回复")}</span>}
    </button>
    {fork && <button type="button" aria-label={t("从这里分支")} title={t("保留到这条回复为止的上下文，创建新会话")}
      disabled={disabled} onClick={() => { void fork().catch(onError); }}><GitFork size={15} /></button>}
    {children}
    {copied && <span className="copy-feedback" role="status">{t("已复制")}</span>}
  </div>;
}
