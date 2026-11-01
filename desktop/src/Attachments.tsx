import { FileText, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { Attachment } from "./types";

export default function Attachments({ items, remove, open, disabled, remote }: {
  items: Attachment[]; remove?: (id: string) => void; open: (item: Attachment) => void; disabled?: boolean; remote?: boolean;
}) {
  if (!items.length) return null;
  return <div className={"attachments " + (remove ? "attachment-draft" : "attachment-sent")} aria-label={remove ? "待发送附件" : "消息附件"}>
    {items.map((item) => <div className="attachment-chip" key={item.id}>
      <button className="attachment-file" disabled={item.available === false} title={item.name} aria-label={(remote && item.kind !== "image" ? "下载附件 " : "查看附件 ") + item.name} onClick={() => open(item)}>
        {item.preview ? <img src={item.preview} alt={item.name} /> : <FileText size={22} />}
        <span><strong>{item.name}</strong><small>{item.available === false ? item.state === "uploading" ? "上传中／待确认" : "上传未完成，请移除后重新添加" : <>{item.size >= 1024 * 1024 ? (item.size / 1024 / 1024).toFixed(1) + " MB" : item.size ? Math.max(1, Math.round(item.size / 1024)) + " KB" : "0 B"}{item.kind === "image" ? " · 图片" : ""}{remote && item.kind !== "image" ? " · 保存副本" : ""}</>}</small></span>
      </button>
      {remove && <button className="attachment-remove" disabled={disabled} aria-label={"移除附件 " + item.name} onClick={() => remove(item.id)}><X size={12} /></button>}
    </div>)}
  </div>;
}

export function AttachmentPreview({ image, close }: { image: { name: string; source: string }; close: () => void }) {
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    button.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); close(); } };
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("keydown", escape); previous?.focus(); };
  }, [close]);
  return createPortal(<div className="overlay" onClick={close}><section className="attachment-preview dialog" role="dialog" aria-modal="true" aria-label="图片附件预览" onClick={(event) => event.stopPropagation()}>
    <header><strong>{image.name}</strong><button ref={button} aria-label="关闭图片预览" onClick={close}><X size={18} /></button></header>
    <img src={image.source} alt={image.name} referrerPolicy="no-referrer" />
  </section></div>, document.body);
}
