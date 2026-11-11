import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { Check, Puzzle } from "lucide-react";
import type { Capabilities, Skill } from "./types";
import { removeSkillMention, skillMention, skillScope } from "./skill-mention";
import { t, tServer } from "./i18n";

export default function SkillInput({ sessionId, draft, selected, capabilities, disabled, placeholder, inputRef, update, send, editPrevious, allowSkills = true }: {
  sessionId: string; draft: string; selected: string[]; capabilities?: Capabilities; disabled: boolean;
  placeholder?: string;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  update: (patch: { draft?: string; selected?: string[]; capabilities?: Capabilities }) => void;
  send: () => void;
  editPrevious?: () => void;
  allowSkills?: boolean;
}) {
  const [caret, setCaret] = useState(draft.length);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const escapeAt = useRef(0);
  const mention = skillMention(draft, caret);
  const signature = JSON.stringify([draft, caret]);
  const open = allowSkills && !!mention && focused && !disabled && dismissed !== signature;
  const options = (capabilities?.skills || []).filter((skill) => skill.valid && skill.enabled && !skill.shadowed &&
    (!mention?.query || (skill.name + " " + skill.description).toLowerCase().includes(mention.query)));
  const index = Math.min(highlight, Math.max(0, options.length - 1));

  useLayoutEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true); setError("");
    // Rescan on each opening so copied/imported SKILL.md files appear without restart.
    window.xuloom.invoke<Capabilities>("capabilities.list", { session: sessionId }).then((value) => {
      if (!cancelled) update({ capabilities: value });
    }).catch((reason) => { if (!cancelled) setError(t("技能目录刷新失败：{value}", { value: String(reason) })); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, sessionId]);
  useEffect(() => { setHighlight(0); }, [mention?.query]);
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [index, open, options.length]);

  function choose(skill: Skill) {
    if (!mention || loading || error) return;
    const value = removeSkillMention(draft, mention);
    update({ draft: value, selected: [...new Set([...selected, skill.name])] });
    setCaret(mention.start); setFocused(true);
    requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.setSelectionRange(mention.start, mention.start);
    });
  }
  return <>
    {open && <div className="skill-picker" aria-label={t("技能选择器")}>
      <div className="skill-picker-heading"><span>{t("选择 Skill")}</span><small>{t("↑ ↓ 选择 · Enter / Tab 确认 · Esc 关闭")}</small></div>
      <div className="skill-picker-list" id="skill-options" role="listbox" aria-label={t("可用技能")} aria-busy={loading} ref={list}>
        {options.map((skill, position) => <button type="button" role="option" id={"skill-option-" + position} key={skill.id}
          aria-selected={position === index} disabled={loading || !!error} title={skill.path}
          onMouseDown={(event) => event.preventDefault()} onMouseMove={() => setHighlight(position)} onClick={() => choose(skill)}>
          <Puzzle size={16} /><span className="skill-picker-name">{skill.name}</span><span className="skill-picker-description">{skill.scope === "builtin" ? tServer(skill.description) : skill.description}</span>
          {selected.includes(skill.name) && <Check size={14} aria-label={t("已选择")} />}<small className="skill-picker-scope">{skillScope(skill.scope)}</small>
        </button>)}
        {!options.length && !loading && !error && <p className="skill-picker-empty">{t("没有匹配的 Skill。可在右侧导入目录，或放入 .xuloom/skills/&lt;名称&gt;/SKILL.md。")}</p>}
      </div>
      {loading && <p className="skill-picker-status" role="status">{t("正在读取技能目录…")}</p>}
      {error && <p className="skill-picker-status error" role="alert">{tServer(error)}</p>}
    </div>}
    <textarea ref={inputRef} aria-label={t("任务输入")} aria-autocomplete="list" aria-controls={open ? "skill-options" : undefined}
      aria-expanded={open} aria-activedescendant={open && options.length ? "skill-option-" + index : undefined}
      placeholder={placeholder ?? (selected.length ? "" : sessionId ? t("和 Xuloom 一起工作，输入 $ 选择 Skill…") : t("点击新建会话即可开始，也可以打开项目…"))}
      disabled={disabled} value={draft}
      onFocus={() => { setFocused(true); setCaret(inputRef.current?.selectionStart || 0); }}
      onBlur={() => setFocused(false)} onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
      onChange={(event) => { setCaret(event.target.selectionStart); update({ draft: event.target.value }); }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === "Backspace" && !event.ctrlKey && !event.metaKey && !event.altKey &&
            allowSkills && selected.length && event.currentTarget.selectionStart === 0 && event.currentTarget.selectionEnd === 0) {
          event.preventDefault();
          update({ selected: selected.slice(0, -1) });
          return;
        }
        if (!open && event.key === "Escape" && !draft.trim()) {
          event.preventDefault();
          if (Date.now() - escapeAt.current < 800) { escapeAt.current = 0; editPrevious?.(); }
          else escapeAt.current = Date.now();
          return;
        }
        if (open) {
          if (event.key === "Escape") { event.preventDefault(); setDismissed(signature); return; }
          if (["ArrowDown", "ArrowUp"].includes(event.key) && options.length) {
            event.preventDefault(); setHighlight((index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length); return;
          }
          if ((event.key === "Enter" && !event.shiftKey) || (event.key === "Tab" && !event.shiftKey && options.length)) {
            event.preventDefault(); if (options[index]) choose(options[index]); return;
          }
        }
        if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); }
      }} />
  </>;
}
