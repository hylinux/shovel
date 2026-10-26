import type { ReactNode } from "react";
import { type Citation } from "./api";

export function Feedback({ error, notice, retry }: { error?: string; notice?: string; retry?: () => void }) {
  return <>{error && <div className="feedback error" role="alert"><span>{error}</span>{retry && <button onClick={retry}>Retry</button>}</div>}{notice && <p className="feedback success" role="status">{notice}</p>}</>;
}
export function Badge({ state }: { state: string }) {
  return <span className={`badge ${["failed", "revoked", "completed_with_errors"].includes(state) ? "bad" : ["ready", "active", "verified", "succeeded", "completed"].includes(state) ? "good" : ""}`}>{state.replaceAll("_", " ")}</span>;
}
export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return <div className="empty"><h3>{title}</h3><p>{children}</p></div>;
}
export function Sources({ items }: { items: Citation[] }) {
  if (!items.length) return null;
  return <section className="sources" aria-label="Sources"><h3>Sources · {items.length}</h3>{items.map((source, index) => <details key={`${source.chunk_id}-${index}`}><summary><span className="source-number">[{source.index ?? index + 1}]</span> {source.title || source.doc_uri}<small>{source.resource_name}</small></summary><p className="preserve">{source.text}</p><dl><dt>Document</dt><dd className="path">{source.doc_uri}</dd><dt>Chunk</dt><dd className="path">{source.chunk_id}</dd><dt>Score</dt><dd>{source.score}</dd></dl></details>)}</section>;
}
