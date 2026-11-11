import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import katex from "katex";
import "katex/dist/katex.min.css";
import { t } from "./i18n";

export default function MathFormula({ text, display }: { text: string; display: boolean }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => { setCopied(false); clearTimeout(timer.current); }, [text]);
  const html = useMemo(() => {
    try {
      if (text.length > 10000) return null;
      return katex.renderToString(text, { displayMode: display, throwOnError: true,
        trust: false, strict: "ignore", maxExpand: 1000, maxSize: 20 });
    } catch { return null; }
  }, [text, display]);
  async function copy() {
    setCopied(false); setCopyError(false); clearTimeout(timer.current);
    try {
      await window.xuloom.invoke("clipboard.writeText", { text });
      setCopied(true); timer.current = setTimeout(() => setCopied(false), 1800);
    } catch { setCopyError(true); }
  }
  return <span className={"math-formula " + (display ? "math-display" : "math-inline")}>
    {html ? <span className="math-render" dangerouslySetInnerHTML={{ __html: html }} />
      : <span className="math-fallback" title={t("公式不完整或包含不支持的 LaTeX，保留原文供复制")}>{text}</span>}
    <button type="button" className="math-copy" aria-label={t("复制公式 LaTeX")} title={copied ? t("已复制") : t("复制公式 LaTeX 原文")}
      onClick={() => void copy()}>{copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
    {copied && <span className="math-copy-feedback" role="status">{t("已复制公式")}</span>}
    {copyError && <span className="math-copy-error" role="alert">{t("复制失败，请重试。")}</span>}
  </span>;
}
