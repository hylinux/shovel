"use client";

import { useI18n } from "./locale-context";

import { useState, type FormEvent } from "react";
import { useAction, type Citation, type Resource, type Status } from "./api";
import { orderSearchResults, type SearchOrder } from "./search-results";
import { pagination } from "./pagination";
import { Empty, Feedback, Pagination, Sources } from "./ui";

const orders: { value: SearchOrder; label: string }[] = [
  { value: "score_desc", label: "Relevance: high to low" },
  { value: "score_asc", label: "Relevance: low to high" },
  { value: "title_asc", label: "Title: A–Z" },
  { value: "title_desc", label: "Title: Z–A" },
];

export function SearchDocuments({ resources, status }: { resources: Resource[]; status?: Status }) {
  const { t } = useI18n();
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = useState("auto");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState("6");
  const [order, setOrder] = useState<SearchOrder>("score_desc");
  const [result, setResult] = useState<{ mode: string; items: Citation[] }>();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const range = pagination(result?.items.length ?? 0, page, pageSize);
  const ordered = orderSearchResults(result?.items ?? [], order);
  const action = useAction();
  const available = resources.filter((resource) => ["active", "degraded", "paused", "verifying"].includes(resource.state));
  const resourceIds = selected.filter((id) => available.some((resource) => resource.id === id));
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const response = await action.run<{ mode: string; items: Citation[] }>("/search", { query: query.trim(), resource_ids: resourceIds, mode, limit: Number(limit) });
    if (response) { setResult(response); setPage(1); }
  }
  return <section>
    <details className="scope-panel"><summary>{t("Knowledge scope ")}<span>{resourceIds.length ? `${resourceIds.length} selected` : t("All accessible resources")}</span></summary>
      <p className="muted">{t("Leave every resource unchecked to search all accessible resources.")}</p>
      <div className="resource-checks">{available.length ? available.map((resource) => <label className="check" key={resource.id}><input type="checkbox" checked={selected.includes(resource.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, resource.id] : current.filter((id) => id !== resource.id))} />{resource.name}</label>) : <p>{t("No accessible resources yet. Add and scan a folder in Resources.")}</p>}</div>
    </details>
    <div className="search-tool"><p className="muted">{t("Search indexed documents directly. No chat model or Memory is used.")}</p>
      <form onSubmit={submit}>
        <div className="search-form"><label className="grow">{t("Search query")}<input value={query} onChange={(event) => setQuery(event.target.value)} required maxLength={8000} placeholder={t("Find a phrase, concept, or document…")} disabled={action.busy} /></label><button className="primary" disabled={action.busy || !query.trim()}>{action.busy ? t("Searching…") : t("Search index")}</button></div>
        <div className="search-options">
          <label>{t("Top N")}<input type="number" min={1} max={30} step={1} required value={limit} onChange={(event) => setLimit(event.target.value)} disabled={action.busy} /></label>
          <div><label htmlFor="search-mode">{t("Search mode")}</label><select id="search-mode" value={mode} onChange={(event) => setMode(event.target.value)} disabled={action.busy}><option value="auto">{t("Auto")}</option><option value="keyword">{t("Keyword")}</option><option value="vector" disabled={!status?.embedding_configured}>{t("Vector")}</option><option value="hybrid" disabled={!status?.embedding_configured}>{t("Hybrid")}</option></select></div>
          <div><label htmlFor="search-order">{t("Sort results")}</label><select id="search-order" value={order} onChange={(event) => {
            const selectedOrder = orders.find((item) => item.value === event.target.value);
            if (selectedOrder) { setOrder(selectedOrder.value); setPage(1); }
          }}>{orders.map((item) => <option key={item.value} value={item.value}>{t(item.label)}</option>)}</select></div>
        </div><p className="muted">{t("Sorting applies to the Top N returned passages.")}</p>
      </form>
      <Feedback error={action.error} />{result && <><p className="muted">{result.items.length}{t(" results · ")}{t(result.mode)}{t(" search")}</p>{result.items.length ? <Sources items={ordered.slice(range.offset, range.end)} startIndex={range.offset} /> : <Empty title={t("No matching passages")}>{t("Try a different query, expand the resource scope, or check that a scan has completed.")}</Empty>}<Pagination label={t("Search results")} pagination={range} onPage={setPage} onPageSize={(size) => { setPageSize(size); setPage(1); }} /></>}
    </div>
  </section>;
}
