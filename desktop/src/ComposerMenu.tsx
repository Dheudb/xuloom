import { useEffect, useRef, useState } from "react";
import { Plus, Paperclip, Target, Lightbulb, Puzzle, Store, Settings2, Clock3, Check, ShieldCheck, Gauge } from "lucide-react";
import { t } from "./i18n";

export default function ComposerMenu({ sessionId, disabled, plan, action }: {
  sessionId: string; disabled: boolean; plan: boolean;
  action: (name: "files" | "goal" | "plan" | "skills" | "mcp" | "settings" | "schedule" | "isolation" | "context") => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => setOpen(false), [sessionId, disabled]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  const options = [
    { name: "files", label: t("文件和图片"), description: t("添加附件，也支持拖入或粘贴"), icon: Paperclip },
    { name: "goal", label: t("目标"), description: t("将下一条消息设为持续执行的目标"), icon: Target },
    { name: "plan", label: t("计划模式"), description: plan ? t("退出计划模式") : t("先制定计划，再决定如何执行"), icon: Lightbulb },
    { name: "schedule", label: t("定时唤醒"), description: t("稍后向此会话投递任务"), icon: Clock3 },
    { name: "isolation", label: t("执行隔离"), description: t("限制工具进程的文件、网络和进程访问"), icon: ShieldCheck },
    { name: "skills", label: "Skills", description: t("选择挂载的技能，也可输入 $"), icon: Puzzle },
    { name: "mcp", label: t("MCP 市场"), description: t("连接和管理外部工具"), icon: Store },
    { name: "settings", label: t("模型设置"), description: t("管理 API 供应商与模型"), icon: Settings2 },
    { name: "context", label: t("上下文"), description: t("查看用量或手动压缩"), icon: Gauge },
  ] as const;
  return <div className="composer-add" ref={root} onKeyDown={(event) => {
    if (!open) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
    else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault(); const items = [...root.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      items[event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length]?.focus();
    } else if (event.key === "Tab") setOpen(false);
  }}>
    <button ref={trigger} className="composer-add-button" type="button" aria-label={t("添加工具与模式")} title={t("添加附件、目标或计划模式")}
      aria-haspopup="menu" aria-expanded={open} disabled={disabled} onClick={() => setOpen(!open)}><Plus size={21} /></button>
    {open && <div className="composer-add-menu" role="menu" aria-label={t("添加工具与模式")}>
      <div className="composer-menu-heading">{t("添加")}</div>
      {options.map(({ name, label, description, icon: Icon }) => <button type="button" role="menuitem" key={name}
        className={name === "skills" ? "composer-menu-divider" : ""} onClick={() => { setOpen(false); trigger.current?.focus(); action(name); }}>
        <Icon size={17} /><span>{label}</span><small>{description}</small>{name === "plan" && plan && <Check size={14} />}
      </button>)}
    </div>}
  </div>;
}
