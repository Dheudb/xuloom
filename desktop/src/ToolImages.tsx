import { useEffect, useRef, useState } from "react";
import { Image as ImageIcon } from "lucide-react";
import type { ToolMedia } from "./types";
import { AttachmentPreview } from "./Attachments";
import { t, tServer } from "./i18n";

function Thumbnail({ media, sessionId, open }: { media: ToolMedia; sessionId: string; open: () => void }) {
  const button = useRef<HTMLButtonElement>(null);
  const [source, setSource] = useState(""), [error, setError] = useState(media.error || "");
  useEffect(() => {
    if (!media.id || media.error || !button.current) return;
    let cancelled = false;
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      observer.disconnect();
      window.xuloom.invoke("media.read", { session: sessionId, id: media.id, thumbnail: true })
        .then(value => { if (!cancelled) setSource(value.source); })
        .catch(reason => { if (!cancelled) setError(String(reason)); });
    });
    observer.observe(button.current);
    return () => { cancelled = true; observer.disconnect(); };
  }, [media.id, media.error, sessionId]);
  return <button ref={button} className="tool-image-thumb" aria-label={t("放大图像 {name}", { name: media.name })} disabled={!!error}
    title={error || `${media.name} · ${media.width} × ${media.height}`} onClick={open}>
    {source ? <img src={source} alt={media.name} /> : <span><ImageIcon size={20} />{error ? t("预览不可用") : t("图片")}</span>}
  </button>;
}

export default function ToolImages({ media, sessionId }: { media?: ToolMedia[]; sessionId: string }) {
  const [image, setImage] = useState<{ name: string; source: string }>(), [error, setError] = useState("");
  if (!media?.length) return null;
  async function open(item: ToolMedia) {
    try {
      const value = await window.xuloom.invoke("media.read", { session: sessionId, id: item.id });
      setImage({ name: item.name, source: value.source }); setError("");
    } catch (reason) { setError(String(reason)); }
  }
  return <><div className="tool-images" aria-label={t("工具返回的图像")}>
    {media.map((item, index) => <Thumbnail key={item.id || index} media={item} sessionId={sessionId} open={() => void open(item)} />)}
  </div>{error && <p className="tool-image-error" role="alert">{tServer(error)}</p>}
    {image && <AttachmentPreview image={image} close={() => setImage(undefined)} />}</>;
}
