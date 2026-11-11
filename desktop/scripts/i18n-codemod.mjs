// Wrap Chinese interface text in t()/tx() so components translate their own text.
//
//   node scripts/i18n-codemod.mjs            rewrite src/ files in place, print the messages found
//   node scripts/i18n-codemod.mjs --check    exit 1 when any file still has unwrapped interface text
//   node scripts/i18n-codemod.mjs --json F   also write {message: [files]} to F
//
// What it rewrites (Chinese text only; everything else is untouched):
//   JSX text runs        <p>已修改 {files.length} 个文件</p>  ->  <p>{tx("已修改 {length} 个文件", { length: files.length })}</p>
//   JSX attributes       aria-label="选择模型"                ->  aria-label={t("选择模型")}
//   strings / templates  "已保存" / `共 ${n} 项`              ->  t("已保存") / t("共 {n} 项", { n })
//   concatenations       "会话操作 " + row.title              ->  t("会话操作 {title}", { title: row.title })
// Left alone: comparisons, switch cases, object keys, type positions, string-method arguments and
// files that hold state or the catalog. Values always enter as placeholders, so they are never translated.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseAst } from "rolldown/parseAst";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HAN = /[一-鿿]/;
const SKIP_FILES = new Set(["i18n.ts", "state.ts"]);
const SKIP_ATTRIBUTES = /^(value|key|id|className|name|type|href|src|data-.*|translate|lang|role|htmlFor|accept|pattern|inputMode|autoComplete)$/;
const STRING_METHODS = new Set(["includes", "startsWith", "endsWith", "indexOf", "lastIndexOf", "has", "split", "replace", "replaceAll", "test", "match", "localeCompare", "get", "set", "delete", "add"]);
// N_() marks a message key that is composed and translated later (gettext convention).
const TRANSLATORS = new Set(["t", "tx", "tServer", "untranslated", "displayName", "N_"]);

export function files() {
  return fs.readdirSync(path.join(root, "src")).filter(name => /\.(tsx|ts)$/.test(name) && !SKIP_FILES.has(name)).map(name => path.join(root, "src", name));
}

/** JSX whitespace rules for one text child (the same cleanup React's compiler applies). */
function cleanJSXText(value) {
  const lines = value.split(/\r\n|\n|\r/);
  let last = 0;
  lines.forEach((line, index) => { if (/[^ \t]/.test(line)) last = index; });
  let result = "";
  lines.forEach((line, index) => {
    let text = line.replace(/\t/g, " ");
    if (index !== 0) text = text.replace(/^[ ]+/, "");
    if (index !== lines.length - 1) text = text.replace(/[ ]+$/, "");
    if (text) { if (index !== last) text += " "; result += text; }
  });
  return result;
}

const quote = text => JSON.stringify(text);

export function transform(source, file = "") {
  const lang = file.endsWith(".tsx") ? "tsx" : "ts";
  const ast = parseAst(source, { lang });
  const messages = [], used = [];
  const slice = node => source.slice(node.start, node.end);

  const children = node => {
    const found = [];
    for (const [key, value] of Object.entries(node)) {
      if (key === "parent") continue;
      if (Array.isArray(value)) value.forEach(item => item && typeof item.type === "string" && found.push(item));
      else if (value && typeof value.type === "string") found.push(value);
    }
    return found.sort((a, b) => a.start - b.start);
  };
  const link = (node, parent) => { node.parent = parent; for (const child of children(node)) link(child, node); };
  link(ast, null);

  const hasHan = node => {
    if (node.type === "Literal" && typeof node.value === "string") return HAN.test(node.value);
    if (node.type === "TemplateLiteral") return node.quasis.some(quasi => HAN.test(quasi.value.cooked ?? quasi.value.raw));
    return false;
  };
  const isString = node => (node.type === "Literal" && typeof node.value === "string") || node.type === "TemplateLiteral";
  const calleeName = call => call.callee.type === "Identifier" ? call.callee.name : call.callee.type === "MemberExpression" && !call.callee.computed ? call.callee.property.name : "";

  /** Positions where a string is data or a key rather than text shown to people. */
  function dataContext(node) {
    let child = node, parent = node.parent;
    // Look through parentheses, ternaries and logical defaults: (a ? "x" : "y") === z is still a comparison.
    while (parent && (parent.type === "ParenthesizedExpression" || parent.type === "ConditionalExpression" && child !== parent.test
      || parent.type === "LogicalExpression")) { child = parent; parent = parent.parent; }
    if (!parent) return false;
    if (parent.type === "BinaryExpression" && /^(===|!==|==|!=|in|instanceof)$/.test(parent.operator)) return true;
    if (parent.type === "SwitchCase" && parent.test === child) return true;
    if ((parent.type === "Property" || parent.type === "PropertyDefinition" || parent.type === "MethodDefinition") && parent.key === child) return true;
    if (parent.type === "MemberExpression" && parent.property === child) return true;
    if (/^(Import|Export)/.test(parent.type)) return true;
    if (/^TS/.test(parent.type)) return true;
    if (parent.type === "CallExpression" && parent.callee !== child) {
      const name = calleeName(parent);
      if (TRANSLATORS.has(name) || STRING_METHODS.has(name) || name === "RegExp") return true;
    }
    if (parent.type === "NewExpression" && parent.callee.type === "Identifier" && /^(RegExp|Set|Map)$/.test(parent.callee.name)) return true;
    if (parent.type === "ArrayExpression" && parent.parent?.type === "NewExpression" && /^(Set|Map)$/.test(parent.parent.callee.name)) return true;
    if (parent.type === "TaggedTemplateExpression") return true;
    if (parent.type === "JSXAttribute") return SKIP_ATTRIBUTES.test(parent.name.name ?? "");
    if (parent.type === "JSXExpressionContainer" && parent.parent?.type === "JSXAttribute") return SKIP_ATTRIBUTES.test(parent.parent.name.name ?? "");
    return false;
  }

  /** Placeholder names: the last identifier of simple expressions, otherwise a position. */
  function namer() {
    const used = new Map();
    return (node, code) => {
      let base = node.type === "Identifier" ? node.name
        : node.type === "MemberExpression" && !node.computed ? node.property.name
        : node.type === "CallExpression" && node.callee.type === "MemberExpression" && !node.callee.computed && node.callee.object.type !== "CallExpression" ? (node.callee.object.type === "Identifier" ? node.callee.object.name : node.callee.object.property?.name ?? "value")
        : "value";
      if (!/^[A-Za-z_]\w*$/.test(base)) base = "value";
      let name = base, index = 2;
      while (used.has(name) && used.get(name) !== code) name = base + index++;
      used.set(name, code);
      return name;
    };
  }
  const objectOf = entries => entries.length ? ", { " + entries.map(([name, code]) => name === code ? name : `${name}: ${code}`).join(", ") + " }" : "";

  /** Flatten a + chain: string pieces become text, other operands placeholders. */
  function flatten(node, out) {
    if (node.type === "BinaryExpression" && node.operator === "+" && stringy(node)) { flatten(node.left, out); flatten(node.right, out); return; }
    if (node.type === "ParenthesizedExpression" && stringy(node.expression) && node.expression.type !== "BinaryExpression") { flatten(node.expression, out); return; }
    if (node.type === "Literal" && typeof node.value === "string") { out.push({ text: node.value }); return; }
    if (node.type === "TemplateLiteral") {
      node.quasis.forEach((quasi, index) => {
        out.push({ text: quasi.value.cooked ?? quasi.value.raw });
        if (index < node.expressions.length) out.push({ node: node.expressions[index] });
      });
      return;
    }
    out.push({ node });
  }
  function stringy(node) {
    if (isString(node)) return true;
    // x + "文字" is a string as soon as either side is one (left-associative, so check the left chain first).
    if (node.type === "BinaryExpression" && node.operator === "+") return stringy(node.left) || isString(node.right);
    return false;
  }
  function chainHasHan(node) {
    const out = [];
    flatten(node, out);
    return out.some(piece => piece.text !== undefined && HAN.test(piece.text));
  }

  /** Build a message from flattened pieces; braces in the text are kept literal by t(). */
  function message(pieces) {
    const name = namer(), values = [];
    let text = "";
    for (const piece of pieces) {
      if (piece.text !== undefined) text += piece.text;
      else {
        const code = emit(piece.node);
        const key = name(piece.node, code);
        if (!values.some(([existing]) => existing === key)) values.push([key, code]);
        text += "{" + key + "}";
      }
    }
    return { text, values };
  }

  function emit(node) {
    const special = rewrite(node);
    if (special !== undefined) return special;
    let code = "", cursor = node.start;
    for (const child of children(node)) {
      if (child.start < cursor) continue;
      const childCode = emit(child), gap = source.slice(cursor, child.start);
      // A removed attribute takes its leading space with it.
      code += (childCode === "" && /^[ \t]+$/.test(gap) ? "" : gap) + childCode;
      cursor = child.end;
    }
    return code + source.slice(cursor, node.end);
  }

  function rewrite(node) {
    // Leave existing translator calls as they are; remember their message keys for the catalog check.
    if (node.type === "CallExpression" && TRANSLATORS.has(calleeName(node)) && node.callee.type === "Identifier") {
      const first = node.arguments[0];
      if (/^(t|tx|N_)$/.test(node.callee.name) && first?.type === "Literal" && typeof first.value === "string") used.push(first.value);
      return slice(node);
    }
    if (node.type === "JSXAttribute" && /^(translate|data-i18n-keep)$/.test(node.name.name ?? "")) return "";
    if (node.type === "JSXAttribute" && node.value?.type === "Literal" && typeof node.value.value === "string" && HAN.test(node.value.value)
      && !SKIP_ATTRIBUTES.test(node.name.name ?? "")) {
      messages.push(node.value.value);
      return `${slice(node.name)}={t(${quote(node.value.value)})}`;
    }
    if (node.type === "JSXElement" || node.type === "JSXFragment") return jsx(node);
    if (node.type === "BinaryExpression" && node.operator === "+" && stringy(node) && chainHasHan(node)
      && !(node.parent?.type === "BinaryExpression" && node.parent.operator === "+" && stringy(node.parent))) {
      if (dataContext(node)) return undefined;
      const pieces = [];
      flatten(node, pieces);
      const { text, values } = message(pieces);
      messages.push(text);
      return `t(${quote(text)}${objectOf(values)})`;
    }
    if (node.type === "TemplateLiteral" && hasHan(node) && node.parent?.type !== "TaggedTemplateExpression"
      && !(node.parent?.type === "BinaryExpression" && node.parent.operator === "+" && stringy(node.parent) && chainHasHan(node.parent))) {
      if (dataContext(node)) return undefined;
      const pieces = [];
      flatten(node, pieces);
      const { text, values } = message(pieces);
      messages.push(text);
      return `t(${quote(text)}${objectOf(values)})`;
    }
    if (node.type === "Literal" && typeof node.value === "string" && HAN.test(node.value)) {
      if (node.parent?.type === "BinaryExpression" && node.parent.operator === "+" && stringy(node.parent) && chainHasHan(node.parent)) return undefined;
      if (dataContext(node) || node.parent?.type === "JSXAttribute") return undefined;
      if (node.parent?.type === "ExpressionStatement" && node.parent.directive) return undefined;
      messages.push(node.value);
      return `t(${quote(node.value)})`;
    }
    return undefined;
  }

  /** JSX children: each run from the first to the last Chinese text becomes one message. */
  function jsx(node) {
    const kids = node.children;
    const textual = kid => kid.type === "JSXText" && HAN.test(kid.value)
      || kid.type === "JSXExpressionContainer" && kid.expression.type === "Literal" && typeof kid.expression.value === "string" && HAN.test(kid.expression.value);
    const indexes = kids.map((kid, index) => textual(kid) ? index : -1).filter(index => index >= 0);
    const openStart = node.type === "JSXElement" ? node.openingElement : node.openingFragment;
    const closeEnd = node.type === "JSXElement" ? node.closingElement : node.closingFragment;
    let code = emit(openStart);
    let cursor = openStart.end;
    if (!indexes.length) {
      for (const kid of kids) { code += source.slice(cursor, kid.start) + emit(kid); cursor = kid.end; }
      return code + source.slice(cursor, closeEnd ? closeEnd.start : node.end) + (closeEnd ? emit(closeEnd) : "");
    }
    // A run covers the sentence around the Chinese text: it extends over neighbouring text and values
    // ("允许执行 {tool}？") and stops at elements on its edges (icons, buttons). Elements between
    // Chinese text become placeholders. Comments ({/* ... */}) cannot be placeholders and split runs.
    const comment = kid => kid.type === "JSXExpressionContainer" && kid.expression.type === "JSXEmptyExpression";
    const element = kid => kid.type === "JSXElement" || kid.type === "JSXFragment";
    const blank = kid => kid.type === "JSXText" && !kid.value.trim();
    const groups = [];
    let group = null;
    kids.forEach((kid, index) => {
      if (comment(kid)) { group = null; return; }
      if (!group) { group = { start: index, end: index }; groups.push(group); } else group.end = index;
    });
    const trimmed = [];
    for (const { start, end } of groups) {
      const han = indexes.filter(index => index >= start && index <= end);
      if (!han.length) continue;
      let from = han[0], to = han[han.length - 1];
      while (from > start && !element(kids[from - 1])) from--;
      while (to < end && !element(kids[to + 1])) to++;
      while (from < to && blank(kids[from])) from++;
      while (to > from && blank(kids[to])) to--;
      trimmed.push({ from, to });
    }
    let index = 0;
    for (const run of trimmed) {
      for (; index < run.from; index++) { code += source.slice(cursor, kids[index].start) + emit(kids[index]); cursor = kids[index].end; }
      const name = namer(), values = [];
      let text = "", lead = "", tail = "";
      for (let at = run.from; at <= run.to; at++) {
        const kid = kids[at];
        if (kid.type === "JSXText") {
          const raw = kid.value;
          let cleaned = cleanJSXText(raw);
          if (at === run.from) { lead = raw.match(/^\s*/)[0]; cleaned = cleaned.replace(/^\s+/, ""); }
          if (at === run.to) { tail = raw.match(/\s*$/)[0]; cleaned = cleaned.replace(/\s+$/, ""); }
          if (at === run.from && at === run.to) { /* both trimmed above */ }
          text += cleaned;
        } else if (kid.type === "JSXExpressionContainer" && kid.expression.type === "Literal" && typeof kid.expression.value === "string") {
          text += kid.expression.value;
        } else {
          const valueNode = kid.type === "JSXExpressionContainer" ? kid.expression : kid;
          const valueCode = emit(valueNode);
          const key = name(valueNode, valueCode);
          if (!values.some(([existing]) => existing === key)) values.push([key, valueCode]);
          text += "{" + key + "}";
        }
      }
      messages.push(text);
      const call = values.length ? `tx(${quote(text)}${objectOf(values)})` : `t(${quote(text)})`;
      // Whitespace around the run stays JSX text; whitespace with a line break is dropped by JSX anyway.
      const keep = space => (space && !/[\r\n]/.test(space) ? space : "");
      code += source.slice(cursor, kids[run.from].start) + keep(lead) + "{" + call + "}" + keep(tail);
      cursor = kids[run.to].end;
      index = run.to + 1;
    }
    for (; index < kids.length; index++) { code += source.slice(cursor, kids[index].start) + emit(kids[index]); cursor = kids[index].end; }
    return code + source.slice(cursor, closeEnd ? closeEnd.start : node.end) + (closeEnd ? emit(closeEnd) : "");
  }

  let output = source.slice(0, ast.start) + emit(ast) + source.slice(ast.end);
  const needsT = /\bt\(/.test(output) && messages.length, needsTx = /\btx\(/.test(output);
  if (messages.length && (needsT || needsTx)) output = ensureImport(output, file, { t: /(^|[^\w.])t\(/.test(output), tx: /(^|[^\w.])tx\(/.test(output) });
  return { output, messages, used, changed: output !== source };
}

function ensureImport(code, file, wanted) {
  const match = /^import\s*\{([^}]*)\}\s*from\s*"\.\/i18n(\.ts)?";?\s*$/m.exec(code);
  const names = new Set(match ? match[1].split(",").map(name => name.trim()).filter(Boolean) : []);
  for (const [name, needed] of Object.entries(wanted)) if (needed) names.add(name);
  const statement = `import { ${[...names].sort().join(", ")} } from "./i18n${file.endsWith(".ts") ? ".ts" : ""}";`;
  if (match) return code.slice(0, match.index) + statement + code.slice(match.index + match[0].length);
  const imports = [...code.matchAll(/^import[^;]*;/gm)];
  if (!imports.length) return statement + "\n" + code;
  const last = imports[imports.length - 1], at = last.index + last[0].length;
  return code.slice(0, at) + "\n" + statement + code.slice(at);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const check = process.argv.includes("--check");
  const jsonAt = process.argv.indexOf("--json");
  const found = {};
  const pending = [], keys = new Set();
  for (const file of files()) {
    const raw = fs.readFileSync(file, "utf8");
    const crlf = raw.includes("\r\n");
    const source = raw.replace(/\r\n/g, "\n");
    const { output, messages, used, changed } = transform(source, file);
    for (const key of used) keys.add(key);
    for (const text of messages) (found[text] ||= []).push(path.basename(file));
    if (changed) {
      pending.push(path.basename(file));
      if (!check) fs.writeFileSync(file, crlf ? output.replace(/\n/g, "\r\n") : output);
    }
  }
  if (jsonAt > 0) fs.writeFileSync(process.argv[jsonAt + 1], JSON.stringify(found, null, 1));
  if (check) {
    if (pending.length) { console.error("Interface text not wrapped in t()/tx() in: " + pending.join(", ") + "\nRun: node scripts/i18n-codemod.mjs"); process.exit(1); }
    const { english } = await import(new URL("../src/locales/en.ts", import.meta.url).href);
    const missing = [...keys].filter(key => HAN.test(key) && !(key in english));
    if (missing.length) { console.error("Messages without English in src/locales/en.ts:\n" + missing.map(key => "  " + JSON.stringify(key)).join("\n")); process.exit(1); }
    console.log(`i18n check passed: ${keys.size} messages wrapped and translated`);
  } else console.log(`${pending.length} files rewritten, ${Object.keys(found).length} messages`);
}
