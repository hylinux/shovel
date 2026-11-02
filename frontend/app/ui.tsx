import type { ReactNode } from "react";
import { type Citation, date } from "./api";
import type { Pagination as PaginationRange } from "./pagination";

export function Feedback({ error, notice, retry }: { error?: string; notice?: string; retry?: () => void }) {
  return <>{error && <div className="feedback error" role="alert"><span>{error}</span>{retry && <button onClick={retry}>Retry</button>}</div>}{notice && <p className="feedback success" role="status">{notice}</p>}</>;
}
export function Badge({ state }: { state: string }) {
  return <span className={`badge ${["failed", "revoked", "completed_with_errors"].includes(state) ? "bad" : ["ready", "active", "verified", "succeeded", "completed"].includes(state) ? "good" : ""}`}>{state.replaceAll("_", " ")}</span>;
}
export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty"><h3>{title}</h3><p>{children}</p></div>;
}
export function Pagination({ label, pagination, onPage, onPageSize }: {
  label: string; pagination: PaginationRange;
  onPage: (page: number) => void; onPageSize: (pageSize: number) => void;
}) {
  if (!pagination.total) return null;
  const { page, pageSize, pageCount, start, end, total } = pagination;
  return <nav className="pagination" aria-label={`${label} pagination`}>
    <p className="muted" role="status">{start}–{end} of {total} {label.toLowerCase()}</p>
    <label>Items per page<select value={pageSize} onChange={(event) => onPageSize(Number(event.target.value))}>
      {[10, 20, 50].map((size) => <option key={size} value={size}>{size}</option>)}
    </select></label>
    <div className="pagination-controls">
      <button type="button" disabled={page === 1} onClick={() => onPage(1)} aria-label={`First page of ${label.toLowerCase()}`}>First</button>
      <button type="button" disabled={page === 1} onClick={() => onPage(page - 1)} aria-label={`Previous page of ${label.toLowerCase()}`}>Previous</button>
      <span className="muted">Page {page} of {pageCount}</span>
      <button type="button" disabled={page === pageCount} onClick={() => onPage(page + 1)} aria-label={`Next page of ${label.toLowerCase()}`}>Next</button>
      <button type="button" disabled={page === pageCount} onClick={() => onPage(pageCount)} aria-label={`Last page of ${label.toLowerCase()}`}>Last</button>
    </div>
  </nav>;
}

export function Sources({ items, collapsible = false, startIndex = 0 }: { items: Citation[]; collapsible?: boolean; startIndex?: number }) {
  if (!items.length) return null;
  const entries = items.map((source, index) => <details key={`${source.chunk_id}-${index}`}><summary><span className="source-number">[{source.index ?? startIndex + index + 1}]</span> {source.title || source.doc_uri}<small>{source.resource_name}</small></summary>{source.email && <dl><dt>From</dt><dd>{source.email.sender}</dd><dt>To</dt><dd>{source.email.to}</dd><dt>Received</dt><dd>{date(source.email.received_at)}</dd><dt>Folder</dt><dd>{source.email.folder_path}</dd></dl>}<p className="preserve">{source.text}</p><dl><dt>Document</dt><dd className="path">{source.doc_uri}</dd><dt>Chunk</dt><dd className="path">{source.chunk_id}</dd><dt>Score</dt><dd>{source.score}</dd></dl></details>);
  return <section className="sources" aria-label="Sources">{collapsible
    ? <details><summary>Sources · {items.length}</summary>{entries}</details>
    : <><h3>Sources · {items.length}</h3>{entries}</>}</section>;
}
