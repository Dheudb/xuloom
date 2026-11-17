import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import Markdown from "./Markdown";
import { t, tServer } from "./i18n";

export const documentPattern = /\.(pdf|docx|docm|dotx|dotm|xlsx|xlsm|xltx|xltm|pptx|pptm|ppsx|ppsm|potx|potm|odt|ods|odp|csv|tsv|json|jsonl|ndjson|yaml|yml|xml|ipynb|svg|tiff?|ico|avif|xps|oxps|epub|fb2|mobi|cbz|zip|rtf)$/i;

type Span = { text: string; bold?: boolean; italic?: boolean };
type Block = { type: string; spans?: Span[]; heading?: boolean; rows?: string[][]; source?: string; text?: string };
type Info = { path: string; version: string; kind: string; pages?: number; sections?: string[]; key: string };
type Page = { kind: string; total: number; limit: number; source?: string; text?: string; blocks?: Block[]; rows?: string[][]; rowNumbers?: string[];
  entries?: { name: string; size: number; directory: boolean }[] };

function columnName(index: number): string {
  let value = index + 1, name = "";
  while (value) { name = String.fromCharCode(65 + (value - 1) % 26) + name; value = Math.floor((value - 1) / 26); }
  return name;
}

function Table({ rows, offset = 0, numbers, indexed = true }: { rows: string[][]; offset?: number; numbers?: string[]; indexed?: boolean }) {
  const columns = Math.max(0, ...rows.map(row => row.length));
  return <div className="document-table-scroll"><table className="document-table">
    {indexed && <thead><tr><th scope="col">#</th>{Array.from({ length: columns }, (_, column) => <th scope="col" key={column}>{columnName(column)}</th>)}</tr></thead>}
    <tbody>{rows.map((row, index) => <tr key={index}>{indexed && <th scope="row">{numbers?.[index] || offset + index + 1}</th>}
      {Array.from({ length: columns }, (_, column) => <td key={column}>{row[column] ?? ""}</td>)}</tr>)}</tbody>
  </table></div>;
}

export default function DocumentPreview({ sessionId, path, revision, onOpen }: {
  sessionId: string; path: string; revision: string; onOpen: (path: string) => void;
}) {
  const key = `${sessionId}:${path}:${revision}`;
  const [info, setInfo] = useState<Info>(), [page, setPage] = useState<Page>();
  const [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const [section, setSection] = useState(0), [offset, setOffset] = useState(0), [zoom, setZoom] = useState(100);
  useEffect(() => {
    let cancelled = false;
    setInfo(undefined); setPage(undefined); setError(""); setLoading(true); setSection(0); setOffset(0); setZoom(100);
    window.xuloom.invoke("files.documentInfo", { session: sessionId, path }).then(value => {
      if (!cancelled) setInfo({ ...value, key });
    }).catch(reason => { if (!cancelled) { setError(String(reason)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [key]);
  useEffect(() => {
    if (!info || info.key !== key) return;
    let cancelled = false;
    setPage(undefined); setError(""); setLoading(true);
    window.xuloom.invoke("files.documentPage", { session: sessionId, path: info.path, version: info.version, section, offset }).then(value => {
      if (!cancelled) setPage(value);
    }).catch(reason => { if (!cancelled) setError(String(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [info, key, section, offset]);
  const paged = info?.kind === "page", sections = info?.sections || [];
  const count = paged ? info?.pages || 0 : page ? Math.ceil(page.total / page.limit) : 0;
  const position = paged ? section : page ? Math.floor(offset / page.limit) : 0;
  const navigate = (value: number) => {
    if (!Number.isInteger(value) || value < 0 || value >= count) return;
    if (paged) setSection(value); else if (page) setOffset(value * page.limit);
  };
  return <div className="document-preview">
    {(paged || sections.length > 1 || count > 1) && <div className="document-toolbar">
      {sections.length > 1 && <select aria-label={t("文档分区")} value={section} onChange={event => { setSection(Number(event.target.value)); setOffset(0); }}>
        {sections.map((name, index) => <option key={index} value={index}>{/\.(pptx|pptm|ppsx|ppsm|potx|potm|odp)$/i.test(path) ? t("幻灯片 {number}", { number: name }) : name}</option>)}
      </select>}
      <button className="icon-button" aria-label={t("上一页")} disabled={loading || position <= 0} onClick={() => navigate(position - 1)}><ChevronLeft size={16} /></button>
      <span className="document-page-count">{t("{current} / {total}", { current: count ? position + 1 : 0, total: count })}</span>
      <button className="icon-button" aria-label={t("下一页")} disabled={loading || position + 1 >= count} onClick={() => navigate(position + 1)}><ChevronRight size={16} /></button>
      {paged && <select aria-label={t("预览缩放")} value={zoom} onChange={event => setZoom(Number(event.target.value))}>
        {[50, 75, 100, 125, 150, 200].map(value => <option key={value} value={value}>{value}%</option>)}
      </select>}
    </div>}
    {info?.kind === "office" && <p className="document-notice">{t("内容预览，复杂版式、图表与公式可能不同；长文本、超宽表格和超大文档有预览上限。ODS 内嵌图片不在表格中显示，完整内容请打开原文件。")}</p>}
    {page && !paged && info?.kind !== "office" && <p className="document-notice">{t("内容已分页，表格最多 100 列；超长单元格与段落会截断。")}</p>}
    {loading && <p role="status">{t("正在渲染文件…")}</p>}
    {error && <p role="alert" className="error">{tServer(error)}</p>}
    {!loading && page && <div className="document-content">
      {page.kind === "page" && <div className="document-page-scroll"><img className="document-page-image" src={page.source} style={{ width: `${zoom}%` }} alt={t("文档第 {number} 页", { number: section + 1 })} /></div>}
      {page.kind === "text" && <pre className="document-code"><code>{page.text}</code></pre>}
      {page.kind === "table" && <Table rows={page.rows || []} offset={offset} numbers={page.rowNumbers} />}
      {page.kind === "archive" && <table className="document-table"><thead><tr><th>{t("文件名")}</th><th>{t("大小（字节）")}</th></tr></thead>
        <tbody>{page.entries?.map((entry, index) => <tr key={index}><td>{entry.name}</td><td>{entry.directory ? "—" : entry.size.toLocaleString()}</td></tr>)}</tbody></table>}
      {page.kind === "document" && <article className="document-paper">{page.blocks?.map((block, index) => {
        if (block.type === "image") return block.source ? <img key={index} src={block.source} alt={t("文档内嵌图片")} loading="lazy" /> : <p className="muted" key={index}>{t("此嵌入对象无法预览。")}</p>;
        if (block.type === "table") return <Table key={index} rows={block.rows || []} indexed={false} />;
        if (block.type === "code") return <pre className="document-code" key={index}><code>{block.text}</code></pre>;
        if (block.type === "markdown") return <div className="markdown" key={index}><Markdown text={block.text || ""} sessionId={sessionId} onOpenFile={onOpen} basePath={path} /></div>;
        const spans = block.spans?.map((span, i) => <span key={i} style={{ fontWeight: span.bold ? 700 : undefined, fontStyle: span.italic ? "italic" : undefined }}>{span.text}</span>);
        return block.heading ? <h3 key={index}>{spans}</h3> : <p key={index}>{spans}</p>;
      })}</article>}
      {page.total === 0 && <p className="muted">{t("文件没有可显示的内容。")}</p>}
    </div>}
  </div>;
}
