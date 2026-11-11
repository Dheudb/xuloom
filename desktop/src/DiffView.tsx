import { t } from "./i18n";
export type DiffLine = { text: string; kind: "added" | "removed" | "context" | "hunk" | "meta"; old?: number; next?: number };
export function diffLines(diff: string): DiffLine[] {
  let old = 0, next = 0, inHunk = false;
  return diff.split("\n").flatMap((text): DiffLine[] => {
    const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
    if (match) { old = Number(match[1]); next = Number(match[2]); inHunk = true; return [{ text, kind: "hunk" }]; }
    if (!inHunk) return [];
    if (text.startsWith("+")) return [{ text: text.slice(1), kind: "added", next: next++ }];
    if (text.startsWith("-")) return [{ text: text.slice(1), kind: "removed", old: old++ }];
    if (text.startsWith(" ")) return [{ text: text.slice(1), kind: "context", old: old++, next: next++ }];
    return text ? [{ text, kind: "meta" }] : [];
  });
}
export default function DiffView({ diff }: { diff: string }) {
  return <div className="file-diff" aria-label={t("文件行级差异")}>{diffLines(diff).map((line, index) => <div key={index} className={"diff-line diff-" + line.kind}>
    <span className="line-number" aria-label={line.old === undefined ? undefined : t("原行 {old}", { old: line.old })}>{line.old ?? ""}</span>
    <span className="line-number" aria-label={line.next === undefined ? undefined : t("新行 {next}", { next: line.next })}>{line.next ?? ""}</span>
    <span className="diff-sign" aria-hidden="true">{line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}</span>
    <code>{line.text || " "}</code>
  </div>)}</div>;
}
