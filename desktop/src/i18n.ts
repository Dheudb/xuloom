// Interface language. Chinese is the source language; English comes from ./locales/en.ts.
//
// Components translate their own interface text by calling t() (strings: attributes, state, code)
// or tx() (JSX sentences that contain inline elements). The Chinese source text is the message key,
// as with gettext, and names, paths and other content enter only through placeholders:
//
//   t("会话操作 {title}", { title: row.title })  ->  "Conversation actions 设置"
//
// Placeholder values are never translated, so user and model content stays exactly as written.
// English messages may use plural forms: "{count, plural, one {# file} other {# files}}".
//
// Text produced outside the renderer (Python bridge and Electron main-process messages) has no
// message key; tServer() translates it from the same catalog when it is a known message and
// leaves anything else unchanged. Use it only for that text, never for user or model content.
//
// `npm run i18n:check` fails when Chinese interface text is not wrapped in t()/tx() or a message
// has no English entry; scripts/i18n-codemod.mjs wraps new text automatically.
import { createElement, Fragment, type ReactNode } from "react";
import { english } from "./locales/en.ts";

export type Language = "zh" | "en";
const STORAGE_KEY = "xuloom.language";
const HAN = /[一-鿿]/;

export function getLanguage(): Language {
  try { return localStorage.getItem(STORAGE_KEY) === "en" ? "en" : "zh"; } catch { return "zh"; }
}

/** Save the choice and reload so every surface is drawn again in the new language. */
export function setLanguage(language: Language) {
  try { localStorage.setItem(STORAGE_KEY, language); } catch { /* storage unavailable: keep the current language */ }
  // The desktop window blocks in-page navigation; the main process performs the reload.
  if (window.xuloom?.reloadWindow) window.xuloom.reloadWindow(); else window.location.reload();
}

const language = getLanguage();
type Values = Record<string, unknown>;

/** Find the closing brace of the block opened at text[open]. */
function closing(text: string, open: number) {
  let depth = 0;
  for (let index = open; index < text.length; index++) {
    if (text[index] === "{") depth++;
    else if (text[index] === "}" && --depth === 0) return index;
  }
  return -1;
}

/** Split a message into literal text and placeholder values (plural blocks are resolved here). */
function parts(message: string, values: Values): unknown[] {
  const result: unknown[] = [];
  let text = "", index = 0;
  while (index < message.length) {
    const open = message.indexOf("{", index);
    if (open < 0) { text += message.slice(index); break; }
    text += message.slice(index, open);
    const end = closing(message, open);
    const body = end < 0 ? "" : message.slice(open + 1, end);
    const simple = /^(\w+)$/.exec(body), plural = /^(\w+),\s*plural,\s*/.exec(body);
    if (simple && simple[1] in values) {
      const value = values[simple[1]];
      if (typeof value === "string" || typeof value === "number" || typeof value === "bigint") text += String(value);
      else { result.push(text, value); text = ""; }
      index = end + 1;
    } else if (plural && plural[1] in values) {
      const count = Number(values[plural[1]]), forms: Record<string, string> = {};
      let rest = body.slice(plural[0].length);
      for (let match = /^\s*(=\d+|zero|one|two|few|many|other)\s*\{/.exec(rest); match; match = /^\s*(=\d+|zero|one|two|few|many|other)\s*\{/.exec(rest)) {
        const start = match[0].length - 1, stop = closing(rest, start);
        if (stop < 0) break;
        forms[match[1]] = rest.slice(start + 1, stop);
        rest = rest.slice(stop + 1);
      }
      const rule = new Intl.PluralRules(language === "en" ? "en" : "zh").select(count);
      const form = forms["=" + count] ?? forms[rule] ?? forms.other ?? "";
      const inner = parts(form.replace(/#/g, String(values[plural[1]])), values);
      for (const piece of inner) {
        if (typeof piece === "string") text += piece; else { result.push(text, piece); text = ""; }
      }
      index = end + 1;
    } else {
      // Not a placeholder (a literal brace in the text): keep it.
      text += "{";
      index = open + 1;
    }
  }
  result.push(text);
  return result.filter(piece => piece !== "");
}

function lookup(message: string) {
  return language === "en" ? english[message] ?? message : message;
}

/** Interface text as a string. Values fill {name} placeholders verbatim. */
export function t(message: string, values: Values = {}): string {
  return parts(lookup(message), values).map(piece => typeof piece === "string" ? piece : String(piece ?? "")).join("");
}

/** Interface text for JSX: values may be elements (links, emphasis, icons) placed inside the sentence. */
export function tx(message: string, values: Values = {}): ReactNode {
  const pieces = parts(lookup(message), values);
  if (pieces.every(piece => typeof piece === "string")) return pieces.join("");
  return createElement(Fragment, null, ...(pieces as ReactNode[]));
}

/** Mark a message key that is combined into a longer message and translated later with t() (gettext's N_). */
export const N_ = (key: string) => key;

/** Text that is deliberately the same in every language (a language's own name, for example). */
export const untranslated = (text: string) => text;

/** Stored names are data. Their wording cannot tell us whether a user chose them. */
export function displayName(name: string): string;
export function displayName(name: string | null | undefined): string | null | undefined;
export function displayName(name: string | null | undefined) {
  return name;
}

/** A conversation title for display: stored titles as written; untitled (or unreadable) ones get the default name. */
export function sessionTitle(row: { title?: string | null; error?: string | null }): string {
  return row.title || (row.error ? t("无法读取的会话") : t("新会话"));
}

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
let fragments: RegExp | undefined;
function fragmentPattern() {
  // Longest keys first so a full sentence wins over the words inside it.
  fragments ||= new RegExp(Object.keys(english).filter(key => HAN.test(key) && !key.includes("{")).sort((a, b) => b.length - a.length).map(escapeRegExp).join("|"), "g");
  return fragments;
}

const PUNCTUATION: [RegExp, string][] = [[/，\s*/g, ", "], [/。\s*/g, ". "], [/：\s*/g, ": "], [/；\s*/g, "; "], [/、\s*/g, ", "],
  [/\s*（/g, " ("], [/）/g, ")"], [/[“”]/g, "\""], [/[‘’]/g, "'"], [/！\s*/g, "! "], [/？\s*/g, "? "], [/…/g, "..."]];

/**
 * English for a message produced by the backend or the Electron main process (errors, notices).
 * Exact catalog matches are used directly; text assembled from known phrases is translated only when
 * every Chinese part is known, otherwise the original is returned (never a half-translated sentence).
 */
export function tServer(text: string): string;
export function tServer(text: string | null | undefined): string | null | undefined;
export function tServer(text: string | null | undefined) {
  if (language !== "en" || !text || !HAN.test(text)) return text;
  const trimmed = text.trim();
  const exact = english[trimmed];
  let result: string;
  if (exact !== undefined) {
    result = exact;
  } else {
    // Paths, URLs and quoted names inside a message are data: hide them from phrase matching.
    const kept: string[] = [];
    const masked = trimmed.replace(DATA_IN_TEXT, match => "" + (kept.push(match) - 1) + "");
    result = masked.replace(fragmentPattern(), match => " " + english[match] + " ");
    if (HAN.test(result)) return text;
    for (const [pattern, replacement] of PUNCTUATION) result = result.replace(pattern, replacement);
    result = result.replace(/\s+([,.;:)!?])/g, "$1").replace(/\(\s+/g, "(").replace(/[ \t]{2,}/g, " ").trim();
    result = result.replace(/(\d+)/g, (_, index) => kept[Number(index)]);
  }
  const lead = text.match(/^\s*/)![0], tail = text.match(/\s*$/)![0];
  return lead + result + tail;
}

// Windows and POSIX paths, URLs, and text in quotes (「」, “”, "") inside backend messages.
const DATA_IN_TEXT = /[a-z][a-z0-9+.-]*:\/\/[^\s，。；]+|(?:[A-Za-z]:)?[\\/][^\s，。；：:"“”「」]+|「[^」]*」|“[^”]*”|"[^"]*"/gi;

/** Set the document language and title. Call once before rendering. */
export function startTranslation() {
  document.documentElement.lang = language === "en" ? "en" : "zh-CN";
  if (HAN.test(document.title)) document.title = tServer(document.title);
}
