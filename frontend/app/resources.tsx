"use client";

import { useState, type FormEvent } from "react";
import { type Connector, type Items, type Operation, type Resource, type Status, date, segment, useAction, useQuery } from "./api";
import { Badge, Empty, Feedback } from "./ui";
import { Profiles } from "./profiles";

export function Resources({ resources, loading, status, refresh }: { resources?: Resource[]; loading: boolean; status?: Status; refresh: () => void }) {
  const connectors = useQuery<Items<Connector>>("/connectors");
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");
  const local = connectors.data?.items.find((connector) => connector.kind === "local_fs");
  const visible = (resources || []).filter((resource) => `${resource.name} ${resource.config.root_path || ""}`.toLowerCase().includes(filter.toLowerCase()));
  return <section>
    <div className="section-toolbar"><div><h2>Connected resources <span className="count">{resources?.length ?? "—"}</span></h2><p>Grant access to a folder, verify it, then build its search index.</p></div><button className="primary" onClick={() => setAdding(!adding)} aria-expanded={adding}>{adding ? "Close form" : "+ Add local folder"}</button></div>
    {adding && <div className="inline-panel"><h3>Add a local folder</h3><Feedback error={connectors.error} retry={connectors.refresh} />{connectors.loading ? <p role="status">Loading connectors…</p> : !local ? <p>Local folders are not available from this runtime.</p> : <><p>{local.description} The path is read by the backend on this machine.</p><CreateResource onCreated={() => { refresh(); setAdding(false); }} /></>}</div>}
    <div className="list-toolbar"><label className="search-field"><span className="sr-only">Filter resources</span><input type="search" placeholder="Filter by name or folder path…" value={filter} onChange={(event) => setFilter(event.target.value)} /></label><span className="muted">Automatically refreshed</span></div>
    {loading && !resources ? <p role="status" className="empty">Loading resources…</p> : !resources ? <Empty title="Resources unavailable">Resolve the connection error above to load your folders.</Empty> : !visible.length ? <Empty title={filter ? "No matching resources" : "Start with a folder"}>{filter ? "Try a different name or path." : "Add a local folder to make its documents available to Shovel. Nothing is indexed until you run a scan."}</Empty> : <div className="resource-list">{visible.map((resource) => <ResourceRow key={resource.id} resource={resource} status={status} refresh={refresh} />)}</div>}
  </section>;
}

function CreateResource({ onCreated }: { onCreated: () => void }) {
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const result = await action.run<Operation>("/resources", { name: String(form.get("name")).trim(), connector_kind: "local_fs", config: { root_path: String(form.get("root_path")).trim() }, description: String(form.get("description")).trim() || undefined, verify: true, scan: false });
    if (result) onCreated();
  }
  return <form onSubmit={submit} className="form-grid"><label>Name<input name="name" required maxLength={200} placeholder="Research notes" autoFocus disabled={action.busy} /></label><label>Folder path<input name="root_path" required placeholder={"C:\\Users\\you\\Documents"} className="path" disabled={action.busy} /></label><label className="full">Description <span className="muted">(optional)</span><input name="description" placeholder="What is in this folder?" disabled={action.busy} /></label><div className="full"><Feedback error={action.error} /><p className="muted">Creation verifies folder access. Scanning is a separate, explicit action.</p><button className="primary" disabled={action.busy}>{action.busy ? "Adding folder…" : "Add and verify"}</button></div></form>;
}

function ResourceRow({ resource, status, refresh }: { resource: Resource; status?: Status; refresh: () => void }) {
  const [scanOpen, setScanOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const action = useAction();
  const base = `/resources/${segment(resource.id)}`;
  async function perform(path: string, method = "POST") {
    const operation = await action.run<Operation>(path, method === "DELETE" ? undefined : {}, method);
    if (operation) { action.setNotice(`${operation.kind.replaceAll("_", " ")} queued. Follow it in Background tasks.`); setConfirmDelete(false); refresh(); }
  }
  return <article className="resource-row">
    <div className="resource-main"><span className="folder-icon" aria-hidden="true">▱</span><div className="resource-info"><div className="title-line"><h3>{resource.name}</h3><Badge state={resource.state} /></div><p className="path">{resource.config.root_path || "No root path provided"}</p>{resource.description && <p>{resource.description}</p>}<p className="resource-meta">{resource.connector_kind} · Last verified {date(resource.last_verified_at)}{resource.authority && ` · ${resource.authority}`}{resource.sensitivity && ` · ${resource.sensitivity}`}</p>{resource.state_reason && <p className="reason">{resource.state_reason}</p>}</div><div className="row-actions"><button onClick={() => setScanOpen(!scanOpen)} disabled={action.busy || ["paused", "revoked"].includes(resource.state)} aria-expanded={scanOpen}>Scan folder</button><details className="action-menu"><summary>Manage<span className="sr-only"> {resource.name}</span></summary><div><button disabled={action.busy} onClick={() => perform(`${base}/verify`)}>Verify access</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/${resource.state === "paused" ? "resume" : "pause"}`)}>{resource.state === "paused" ? "Resume" : "Pause"}</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/revoke`)}>Revoke access</button><button className="danger" disabled={action.busy} onClick={() => setConfirmDelete(true)}>Delete resource…</button></div></details></div></div>
    <Feedback error={action.error} notice={action.notice} />
    <button onClick={() => setProfilesOpen(!profilesOpen)} aria-expanded={profilesOpen}>Manage scan profiles</button>
    {profilesOpen && <Profiles resourceId={resource.id} />}
    {confirmDelete && <div className="confirmation" role="alert"><p>Delete <strong>{resource.name}</strong>? This removes the resource and its index, not your original files.</p><div className="button-row"><button className="danger" disabled={action.busy} onClick={() => perform(base, "DELETE")}>{action.busy ? "Deleting…" : "Delete resource"}</button><button disabled={action.busy} onClick={() => setConfirmDelete(false)}>Keep resource</button></div></div>}
    {scanOpen && <ScanForm key={status?.embedding_configured ? "embed" : "keyword"} resource={resource} embeddingConfigured={Boolean(status?.embedding_configured)} onQueued={() => { refresh(); setScanOpen(false); action.setNotice("Scan queued. Follow its progress in Background tasks."); }} />}
  </article>;
}

function ScanForm({ resource, embeddingConfigured, onQueued }: { resource: Resource; embeddingConfigured: boolean; onQueued: () => void }) {
  const profiles = useQuery<Items<{ name: string; is_enabled: boolean }>>(`/resources/${segment(resource.id)}/profiles`);
  const [profile, setProfile] = useState("default");
  const [embed, setEmbed] = useState(embeddingConfigured);
  const [mode, setMode] = useState("incremental");
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const limit = String(form.get("limit") || "");
    const result = await action.run<Operation>(`/resources/${segment(resource.id)}/scans`, { mode, embed, profile_name: profile, force: form.get("force") === "on", ...(limit ? { limit: Number(limit) } : {}), ...(mode === "targeted" ? { target_ids: String(form.get("targets")).split(/\r?\n/).map((value) => value.trim()).filter(Boolean) } : {}) });
    if (result) onQueued();
  }
  return <form className="inline-panel form-grid" onSubmit={submit}>
    <h4 className="full">Scan {resource.name}</h4>
    <label>Profile<select value={profile} onChange={(event) => setProfile(event.target.value)} disabled={action.busy || profiles.loading}>
      {(profiles.data?.items || [{ name: "default", is_enabled: true }]).map((item) => <option key={item.name} value={item.name} disabled={!item.is_enabled}>{item.name}{!item.is_enabled && " (disabled)"}</option>)}
    </select></label>
    <Feedback error={profiles.error} retry={profiles.refresh} />
    <label>Scan mode<select value={mode} onChange={(event) => setMode(event.target.value)} disabled={action.busy}>
      <option value="incremental">Incremental — changed documents</option><option value="full_sweep">Full sweep — reconcile folder</option><option value="targeted">Targeted — selected identifiers</option>
    </select></label>
    <label>Document limit <span className="muted">(optional)</span><input type="number" name="limit" min="1" step="1" disabled={action.busy} /></label>
    {mode === "targeted" && <label className="full">Target identifiers, one per line<textarea name="targets" required disabled={action.busy} /></label>}
    <div className="full"><label className="check"><input type="checkbox" checked={embed} onChange={(event) => setEmbed(event.target.checked)} disabled={!embeddingConfigured || action.busy} />Generate embeddings</label>
      <p className="muted">{embeddingConfigured ? "Turn off for a keyword-only scan. Keyword search works without vectors." : "No embedding provider configured. This scan builds a keyword index; vectors are not required."}</p>
      <label className="check"><input type="checkbox" name="force" disabled={action.busy} />Reprocess unchanged documents</label><Feedback error={action.error} />
      <button className="primary" disabled={action.busy || Boolean(profiles.error)}>{action.busy ? "Queuing scan…" : "Start scan"}</button>
    </div>
  </form>;
}
