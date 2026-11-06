"use client";

import { useI18n } from "./locale-context";
import type { ReactNode } from "react";
import { type Citation } from "./api";
import type { Pagination as PaginationRange } from "./pagination";

export function Feedback({ error, notice, retry }: { error?: string; notice?: string; retry?: () => void }) {
  const { t } = useI18n();
  return <>{error && <div className="feedback error" role="alert"><span>{t(error)}</span>{retry && <button onClick={retry}>{t("Retry")}</button>}</div>}{notice && <p className="feedback success" role="status">{t(notice)}</p>}</>;
}
export function Badge({ state }: { state: string }) {
  const { t } = useI18n();
  return <span className={`badge ${["failed", "revoked", "completed_with_errors"].includes(state) ? "bad" : ["ready", "active", "verified", "succeeded", "completed"].includes(state) ? "good" : ""}`}>{t(state.replaceAll("_", " "))}</span>;
}
export function Empty({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useI18n();
  return <div className="empty"><h3>{t(title)}</h3><p>{children}</p></div>;
}
export function Pagination({ label, pagination, onPage, onPageSize }: {
  label: string; pagination: PaginationRange;
  onPage: (page: number) => void; onPageSize: (pageSize: number) => void;
}) {
  const { t } = useI18n();
  if (!pagination.total) return null;
  const { page, pageSize, pageCount, start, end, total } = pagination;
  return <nav className="pagination" aria-label={t("{p0} pagination", { p0: t(label) })}>
    <p className="muted" role="status">{start}–{end}{t(" of ")}{total} {t(label.toLowerCase())}</p>
    <label>{t("Items per page")}<select value={pageSize} onChange={(event) => onPageSize(Number(event.target.value))}>
      {[10, 20, 50].map((size) => <option key={size} value={size}>{size}</option>)}
    </select></label>
    <div className="pagination-controls">
      <button type="button" disabled={page === 1} onClick={() => onPage(1)} aria-label={t("First page of {p0}", { p0: label.toLowerCase() })}>{t("First")}</button>
      <button type="button" disabled={page === 1} onClick={() => onPage(page - 1)} aria-label={t("Previous page of {p0}", { p0: label.toLowerCase() })}>{t("Previous")}</button>
      <span className="muted">{t("Page ")}{page}{t(" of ")}{pageCount}</span>
      <button type="button" disabled={page === pageCount} onClick={() => onPage(page + 1)} aria-label={t("Next page of {p0}", { p0: label.toLowerCase() })}>{t("Next")}</button>
      <button type="button" disabled={page === pageCount} onClick={() => onPage(pageCount)} aria-label={t("Last page of {p0}", { p0: label.toLowerCase() })}>{t("Last")}</button>
    </div>
  </nav>;
}

export function Sources({ items, collapsible = false, startIndex = 0 }: { items: Citation[]; collapsible?: boolean; startIndex?: number }) {
  const { t, date } = useI18n();
  if (!items.length) return null;
  const entries = items.map((source, index) => <details key={`${source.chunk_id}-${index}`}><summary><span className="source-number">[{source.index ?? startIndex + index + 1}]</span> {source.title || source.doc_uri}<small>{source.resource_name}</small></summary>{source.email && <dl><dt>{t("From")}</dt><dd>{source.email.sender}</dd><dt>{t("To")}</dt><dd>{source.email.to}</dd><dt>{t("Received")}</dt><dd>{date(source.email.received_at)}</dd><dt>{t("Folder")}</dt><dd>{source.email.folder_path}</dd></dl>}<p className="preserve">{source.text}</p><dl><dt>{t("Document")}</dt><dd className="path">{source.doc_uri}</dd><dt>{t("Chunk")}</dt><dd className="path">{source.chunk_id}</dd><dt>{t("Score")}</dt><dd>{source.score}</dd></dl></details>);
  return <section className="sources" aria-label={t("Sources")}>{collapsible
    ? <details><summary>{t("Sources · ")}{items.length}</summary>{entries}</details>
    : <><h3>{t("Sources · ")}{items.length}</h3>{entries}</>}</section>;
}
