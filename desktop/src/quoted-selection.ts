export type QuotedSelection = { text: string; source: string; owner: string };
export const QUOTE_LIMIT = 16000;
export function quoteDraft(draft: string, quote: QuotedSelection): string {
  // User-selected content stays user data; no system instructions or hidden prompt.
  const block = "> " + quote.source.replace(/[\r\n]/g, " ") + "\n>\n" +
    quote.text.split(/\r?\n/).map(line => "> " + line).join("\n");
  return draft + (draft ? "\n\n" : "") + block + "\n\n";
}
