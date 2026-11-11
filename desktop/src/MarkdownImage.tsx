import { createContext, useContext, useEffect, useState } from "react";
import { imageReference } from "./image-reference";
import { AttachmentPreview } from "./Attachments";
import { t } from "./i18n";

export const MarkdownImageContext = createContext<{ sessionId?: string; basePath?: string }>({});

export default function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const { sessionId, basePath } = useContext(MarkdownImageContext);
  const [source, setSource] = useState(""), [failed, setFailed] = useState(false), [open, setOpen] = useState(false), [ready, setReady] = useState(false);
  const name = alt || t("图片");
  useEffect(() => {
    let cancelled = false;
    setSource(""); setFailed(false); setOpen(false); setReady(false);
    const reference = imageReference(src);
    if (reference.kind === "invalid") setFailed(true);
    else if (reference.kind !== "local") setSource(reference.source);
    else if (!sessionId) setFailed(true);
    else window.xuloom.invoke("files.image", { session: sessionId, path: reference.source, basePath })
      .then(value => { if (!cancelled) setSource(value.source); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [src, sessionId, basePath]);
  if (failed || !source) return null;
  return <><button type="button" className="markdown-image" style={{ opacity: ready ? 1 : 0 }} disabled={!ready} aria-hidden={!ready} title={t("点击放大")} aria-label={t("放大图片 {name}", { name })} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setOpen(true); }}>
    <img src={source} alt={name} loading="lazy" referrerPolicy="no-referrer" onLoad={() => setReady(true)} onError={() => setFailed(true)} />
  </button>{open && source && <AttachmentPreview image={{ name, source }} close={() => setOpen(false)} />}</>;
}
