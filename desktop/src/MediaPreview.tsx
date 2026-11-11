import { useEffect, useState } from "react";
import { t, tServer } from "./i18n";

export default function MediaPreview({ sessionId, path, mime, revision }: { sessionId: string; path: string; mime: string; revision: string }) {
  const [source, setSource] = useState(""), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const image = mime.startsWith("image/"), video = mime.startsWith("video/");
  useEffect(() => {
    let cancelled = false, token = "";
    const release = (value: string) => { void window.xuloom.invoke("media-preview.close", { session: sessionId, token: value }).catch(() => {}); };
    setSource(""); setError(""); setLoading(true);
    window.xuloom.invoke(image ? "files.image" : "media-preview.open", { session: sessionId, path }).then(value => {
      token = value.token || "";
      if (cancelled) { if (token) release(token); }
      else setSource(image ? value.source : value.url);
    }).catch(reason => { if (!cancelled) { setError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; if (token) release(token); };
  }, [sessionId, path, mime, revision]);
  const failed = () => { setError(t("此媒体无法解码，或文件已变化/连接已断开。请刷新后重试。")); setLoading(false); };
  return <div className="media-preview">
    {loading && <p role="status" className="muted">{t("正在加载媒体预览…")}</p>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {source && !error && (image ? <img alt={t("文件图片预览")} src={source} onLoad={() => setLoading(false)} onError={failed} />
      : video ? <video aria-label={t("视频预览")} src={source} controls playsInline preload="metadata" onLoadedMetadata={() => setLoading(false)} onError={failed} />
      : <audio aria-label={t("音频预览")} src={source} controls preload="metadata" onLoadedMetadata={() => setLoading(false)} onError={failed} />)}
    {!image && source && <p className="fine-print">{t("读取当前执行机器上的文件，关闭预览即撤销播放地址。")}</p>}
  </div>;
}
