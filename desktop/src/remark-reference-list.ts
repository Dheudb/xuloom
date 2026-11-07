import type { List, Paragraph, PhrasingContent, Root } from "mdast";

// Some replies put an entire bibliography in one paragraph. Split only plain
// numbered markers, leaving link labels, code, math and ordinary citations alone.
function referenceList(paragraph: Paragraph, explicitSources: boolean): List | undefined {
  const first = paragraph.children[0];
  if (first?.type !== "text" || !/^\s*\[1\]\s/.test(first.value)) return;
  if (!explicitSources && !paragraph.children.some(node => node.type === "link" && /^https?:\/\//i.test(node.url))) return;
  const rows: { number: number; children: PhrasingContent[] }[] = [];
  for (const child of paragraph.children) {
    if (child.type !== "text") { rows.at(-1)?.children.push(child); continue; }
    let offset = 0;
    for (const match of child.value.matchAll(/\[(\d{1,4})\]\s+/g)) {
      if (match.index > 0 && !/\s/.test(child.value[match.index - 1])) continue;
      if (match.index > offset) rows.at(-1)?.children.push({ type: "text", value: child.value.slice(offset, match.index) });
      rows.push({ number: Number(match[1]), children: [] });
      offset = match.index + match[0].length;
    }
    if (offset < child.value.length) rows.at(-1)?.children.push({ type: "text", value: child.value.slice(offset) });
  }
  if (rows.length < 2 || rows.some((row, index) => row.number !== index + 1)) return;
  for (const row of rows) {
    while (row.children[0]?.type === "text") {
      const value = row.children[0].value.trimStart();
      if (value) { row.children[0] = { type: "text", value }; break; }
      row.children.shift();
    }
    while (row.children.at(-1)?.type === "text") {
      const last = row.children.at(-1) as { type: "text"; value: string };
      const value = last.value.trimEnd();
      if (value) { row.children[row.children.length - 1] = { type: "text", value }; break; }
      row.children.pop();
    }
    if (!row.children.length) return;
  }
  return {
    type: "list", ordered: true, start: 1, spread: false,
    data: { hProperties: { className: ["reference-list"], ariaLabel: "参考来源" } },
    children: rows.map(row => ({ type: "listItem", spread: false,
      children: [{ type: "paragraph", children: row.children }] })),
  };
}

export default function remarkReferenceList() {
  return (tree: Root) => {
    tree.children = tree.children.map((node, index, nodes) => {
      if (node.type !== "paragraph") return node;
      const previous = nodes[index - 1];
      const explicitSources = previous?.type === "thematicBreak" || previous?.type === "heading" &&
        /^(?:references|sources|bibliography|参考文献|参考来源|资料来源|来源|引用)\s*[:：]?$/i.test(
          previous.children.map(child => child.type === "text" ? child.value : "").join(""));
      return referenceList(node, explicitSources) || node;
    });
  };
}
