import { t } from "./i18n.ts";
// A mention is a complete token at the caret, not an arbitrary dollar in prose.
export function skillMention(text: string, caret: number) {
  const before = text.slice(0, caret);
  const match = /(?:^|\s)\$([\p{L}\p{N}-]*)$/u.exec(before);
  if (!match || /^\d/.test(match[1])) return null;
  const start = caret - match[1].length - 1;
  const end = caret + (/^[\p{L}\p{N}-]*/u.exec(text.slice(caret))?.[0].length || 0);
  return { start, end, query: text.slice(start + 1, end).toLowerCase() };
}

export function removeSkillMention(text: string, mention: { start: number; end: number }) {
  return text.slice(0, mention.start) + text.slice(mention.end).replace(/^ /, "");
}

export const skillScope = (scope: string) =>
  ({ project: t("项目"), global: t("全局"), imported: t("导入"), environment: t("环境"), builtin: t("内置") })[scope] || scope;
