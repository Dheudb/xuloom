import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import type { Root, RootContent } from "mdast";
import type { VFile } from "vfile";
import MathFormula from "./MathFormula";
import MarkdownImage, { MarkdownImageContext } from "./MarkdownImage";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Check, Copy } from "lucide-react";

// remark-math treats same-line $$...$$ as inline; retain the author's display intent.
function remarkDisplayMath() {
  return (tree: Root, file: VFile) => {
    const source = String(file);
    function visit(node: Root | RootContent) {
      if (node.type === "inlineMath" && source.slice(node.position?.start.offset).startsWith("$$")) {
        node.data = { ...node.data, hProperties: { className: ["language-math", "math-display"] } };
      }
      if ("children" in node) node.children.forEach(visit);
    }
    visit(tree);
  };
}

function CodeBlock({ text, language }: { text: string; language: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => { setCopied(false); clearTimeout(timer.current); }, [text]);
  async function copy() {
    setError(""); setCopied(false); clearTimeout(timer.current);
    try {
      await window.xuloom.invoke("clipboard.writeText", { text });
      setCopied(true); clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1800);
    } catch { setError("复制失败，请重试或选中代码手动复制。"); }
  }
  return <div className="markdown-code-block">
    <div className="code-block-toolbar">
      <span className="code-block-language">{language || "代码"}</span>
      <button type="button" aria-label="复制代码" title="复制整个代码块" onClick={() => void copy()}>
        {copied ? <Check size={14} /> : <Copy size={14} />}
        <span role={copied ? "status" : undefined}>{copied ? "已复制" : "复制代码"}</span>
      </button>
    </div>
    {error && <div className="code-copy-error" role="alert">{error}</div>}
    <pre><code className={language ? "language-" + language : undefined}>{text}</code></pre>
  </div>;
}

function webpageURL(href?: string) {
  try {
    const url = new URL(href || "");
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function chatURL(value?: string) {
  return value && value.length <= 512 && !/[\r\n]/.test(value) && /^xuloom:\/\/chat\/(v1\/[0-9a-f]{16}|ssh\/v1\/[0-9a-f]{64})\/[A-Za-z0-9_-]{1,128}$/.test(value) ? value : null;
}

function fileReference(value: string) {
  return value.length <= 1000 && !/[\n\r<>]/.test(value) && /\.[a-z0-9]{1,12}$/i.test(value) &&
    (!/^[a-z][\w+.-]*:/i.test(value) || /^[a-z]:[\\/]/i.test(value));
}
function localLink(value?: string) {
  if (!value) return null;
  try { value = decodeURIComponent(value); } catch { return null; }
  return fileReference(value) ? value : null;
}
export default function Markdown({ text, onOpenFile, sessionId, basePath }: { text: string; onOpenFile?: (path: string) => void; sessionId?: string; basePath?: string }) {
  const [linkError, setLinkError] = useState("");
  function openLink(event: MouseEvent<HTMLAnchorElement>, url: string) {
    event.preventDefault();
    setLinkError("");
    window.xuloom.invoke("external.open", { url }).catch(() => setLinkError("无法打开链接，请复制地址后手动打开：" + url));
  }
  return (
    <>
    <MarkdownImageContext.Provider value={{ sessionId, basePath }}>
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath, remarkDisplayMath]}
      urlTransform={(url, key) => key === "src" ? url : webpageURL(url) || chatURL(url) || (onOpenFile && fileReference(url) ? url : "")}
      components={{
        pre: ({ children, node }) => {
          const code = node?.children[0];
          if (code?.type !== "element" || code.tagName !== "code") return <pre>{children}</pre>;
          const text = code.children.map((child) => child.type === "text" ? child.value : "").join("");
          const classes = code.properties.className;
          const language = (Array.isArray(classes) ? classes.map(String) : String(classes || "").split(" "))
            .find((name) => name.startsWith("language-"))?.slice(9) || "";
          if (Array.isArray(classes) && classes.includes("math-display")) return <MathFormula text={text.replace(/\n$/, "")} display />;
          return <CodeBlock text={text} language={language} />;
        },
        code: ({ children, className, node }) => {
          const path = String(children);
          if (className?.split(" ").some((name) => name === "math-inline" || name === "math-display")) {
            return <MathFormula text={path} display={className.split(" ").includes("math-display")} />;
          }
          return onOpenFile && !className && node?.position?.start.line === node?.position?.end.line && fileReference(path)
            ? <button className="inline-file-link" title={"查看文件 " + path} onClick={() => onOpenFile(path)}><code>{children}</code></button>
            : <code className={className}>{children}</code>;
        },
        a: ({ children, href }) => {
          const url = webpageURL(href) || chatURL(href);
          const path = localLink(href);
          return url ? <a href={url} title={url} className="markdown-link" target="_blank" rel="noopener noreferrer"
            onClick={(event) => openLink(event, url)}
            onAuxClick={(event) => { if (event.button === 1) openLink(event, url); }}>{children}</a>
            : onOpenFile && path ? <button className="inline-file-link" onClick={() => onOpenFile(path)}>{children}</button>
            : <span title={href}>{children}</span>;
        },
        img: MarkdownImage,
      }}
    >
      {text}
    </ReactMarkdown>
    </MarkdownImageContext.Provider>
    {linkError && <p className="muted markdown-link-error" role="alert">{linkError}</p>}
    </>
  );
}
