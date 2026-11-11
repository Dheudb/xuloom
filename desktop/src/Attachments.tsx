import { FileText, X } from "lucide-react";
import { useDialogFocus } from './useDialogFocus';
import { createPortal } from "react-dom";
import type { Attachment } from "./types";
import { t } from "./i18n";

export default function Attachments({ items, remove, open, disabled, remote }: {
  items: Attachment[]; remove?: (id: string) => void; open: (item: Attachment) => void; disabled?: boolean; remote?: boolean;
}) {
  if (!items.length) return null;
  return <div className={"attachments " + (remove ? "attachment-draft" : "attachment-sent")} aria-label={remove ? t("待发送附件") : t("消息附件")}>
    {items.map((item) => <div className="attachment-chip" key={item.id}>
      <button className="attachment-file" disabled={item.available === false} title={item.name} aria-label={(remote && item.kind !== "image" ? t("下载附件 ") : t("查看附件 ")) + item.name} onClick={() => open(item)}>
        {item.preview ? <img src={item.preview} alt={item.name} /> : <FileText size={22} />}
        <span><strong>{item.name}</strong><small>{item.available === false ? item.state === "uploading" ? t("上传中／待确认") : t("上传未完成，请移除后重新添加") : <>{item.size >= 1024 * 1024 ? (item.size / 1024 / 1024).toFixed(1) + " MB" : item.size ? Math.max(1, Math.round(item.size / 1024)) + " KB" : "0 B"}{item.kind === "image" ? t(" · 图片") : ""}{remote && item.kind !== "image" ? t(" · 保存副本") : ""}</>}</small></span>
      </button>
      {remove && <button className="attachment-remove" disabled={disabled} aria-label={t("移除附件 {name}", { name: item.name })} onClick={() => remove(item.id)}><X size={12} /></button>}
    </div>)}
  </div>;
}

export function AttachmentPreview({ image, close }: { image: { name: string; source: string }; close: () => void }) {
  const dialog=useDialogFocus();
  return createPortal(<div className="overlay" onClick={close}><section ref={dialog} className="attachment-preview dialog" role="dialog" aria-modal="true" aria-label={t("图片附件预览")} onClick={(event) => event.stopPropagation()} onKeyDown={e=>{if(e.key==='Escape'){e.stopPropagation();close();}}}>
    <header><strong>{image.name}</strong><button aria-label={t("关闭图片预览")} onClick={close}><X size={18} /></button></header>
    <img src={image.source} alt={image.name} referrerPolicy="no-referrer" />
  </section></div>, document.body);
}
