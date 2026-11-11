import { useEffect, useState } from "react";
import { t, tServer } from "./i18n";

export default function HtmlPreview({ sessionId, path, revision }: { sessionId: string; path: string; revision: string }) {
  const [url, setUrl] = useState(""), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  useEffect(() => {
    let cancelled = false, token = "";
    const release = (value: string) => { void window.xuloom.invoke("preview.close", { session: sessionId, token: value }).catch(() => {}); };
    setUrl(""); setError(""); setLoading(true);
    window.xuloom.invoke("preview.open", { session: sessionId, path }).then((value) => {
      token = value.token;
      if (cancelled) release(token); else setUrl(value.url);
    }).catch((reason) => { if (!cancelled) { setError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; if (token) release(token); };
  }, [sessionId, path, revision]);
  return <div className="html-preview">
    {loading && <p className="muted" role="status">{t("正在加载网页预览…")}</p>}
    {error && <p className="error" role="alert">{tServer(error)}</p>}
    {url && <iframe key={url} title={t("HTML 网页预览")} src={url} sandbox="allow-scripts allow-same-origin" referrerPolicy="no-referrer" onLoad={() => setLoading(false)} />}
  </div>;
}
