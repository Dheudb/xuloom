import { useEffect, useRef, useState } from "react";
import { t } from "./i18n";
import { QUOTE_LIMIT, type QuotedSelection } from "./quoted-selection";

export default function SelectionActions({ scope, add, side, canSide, onError }: {
  scope: string; add: (quote: QuotedSelection) => void; side: (quote: QuotedSelection) => void;
  canSide: (owner: string) => boolean; onError: (error: unknown) => void;
}) {
  const [selected, setSelected] = useState<{ quote: QuotedSelection; x: number; y: number }>();
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setSelected(undefined);
    const capture = (event: MouseEvent) => {
      if (menu.current?.contains(event.target as Node) || event.type === "mouseup" && event.button !== 0) return;
      const selection = window.getSelection();
      const element = (node: Node | null) => node?.nodeType === Node.ELEMENT_NODE ? node as Element : node?.parentElement;
      const start = element(selection?.anchorNode || null)?.closest<HTMLElement>("[data-quote-source]");
      const end = element(selection?.focusNode || null)?.closest<HTMLElement>("[data-quote-source]");
      const text = selection?.toString().trim();
      if (!text || !start || start !== end || (event.target as Element).closest("input,textarea,[contenteditable=true]")) {
        setSelected(undefined); return;
      }
      if (event.type === "contextmenu") event.preventDefault();
      if (text.length > QUOTE_LIMIT) { setSelected(undefined); onError(t("选中文字过长，请缩小选区后重试。")); return; }
      const rect = selection!.getRangeAt(0).getBoundingClientRect();
      setSelected({ quote: { text, source: start.dataset.quoteSource || "", owner: start.dataset.quoteOwner || scope },
        x: Math.max(8, Math.min(event.type === "contextmenu" ? event.clientX : rect.left, window.innerWidth - 300)),
        y: Math.max(8, Math.min(event.type === "contextmenu" ? event.clientY : rect.bottom + 6, window.innerHeight - 58)) });
    };
    const clear = () => setSelected(undefined);
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") clear(); };
    document.addEventListener("mouseup", capture); document.addEventListener("contextmenu", capture);
    document.addEventListener("keydown", key); window.addEventListener("resize", clear);
    document.addEventListener("scroll", clear, true);
    return () => { document.removeEventListener("mouseup", capture); document.removeEventListener("contextmenu", capture);
      document.removeEventListener("keydown", key); window.removeEventListener("resize", clear); document.removeEventListener("scroll", clear, true); };
  }, [scope]);
  if (!selected) return null;
  const choose = (callback: (quote: QuotedSelection) => void) => {
    callback(selected.quote); setSelected(undefined); window.getSelection()?.removeAllRanges();
  };
  return <div ref={menu} className="selection-actions" role="toolbar" aria-label={t("选中文字操作")}
    style={{ left: selected.x, top: selected.y }} onMouseDown={event => event.preventDefault()}>
    <button onClick={() => choose(add)}>{t("添加到对话")}</button>
    {canSide(selected.quote.owner) && <button onClick={() => choose(side)}>{t("在侧边聊天中提问")}</button>}
  </div>;
}
