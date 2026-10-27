"use client";

import { useState, type FormEvent } from "react";
import { type Connector, type Items, type Operation, type OutlookFolder, type OutlookStore, type Resource, type Status, date, segment, useAction, useQuery } from "./api";
import { Badge, Empty, Feedback } from "./ui";
import { Profiles } from "./profiles";

export function Resources({ resources, loading, status, refresh }: { resources?: Resource[]; loading: boolean; status?: Status; refresh: () => void }) {
  const connectors = useQuery<Items<Connector>>("/connectors");
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");
  const [kind, setKind] = useState("local_fs");
  const connector = connectors.data?.items.find((connector) => connector.kind === kind);
  const visible = (resources || []).filter((resource) => `${resource.name} ${resource.config.root_path || ""} ${resource.connector_kind} ${resource.description || ""}`.toLowerCase().includes(filter.toLowerCase()));
  return <section>
    <div className="section-toolbar"><div><h2>Connected resources <span className="count">{resources?.length ?? "—"}</span></h2><p>Choose local documents or classic Outlook mail, verify access, then build its search index.</p></div><button className="primary" onClick={() => setAdding(!adding)} aria-expanded={adding}>{adding ? "Close form" : "+ Add resource"}</button></div>
    {adding && <div className="inline-panel"><h3>Add a resource</h3><Feedback error={connectors.error} retry={connectors.refresh} />{connectors.loading ? <p role="status">Loading connectors…</p> : <><label>Resource type<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="local_fs">Local folder</option><option value="outlook_local">Classic Outlook mail (Windows)</option></select></label>{!connector ? <p>This resource type is not available from this runtime.</p> : <><p>{connector.description}</p>{kind === "outlook_local" ? <CreateOutlookResource onCreated={() => { refresh(); setAdding(false); }} /> : <CreateResource onCreated={() => { refresh(); setAdding(false); }} />}</>}</>}</div>}
    <div className="list-toolbar"><label className="search-field"><span className="sr-only">Filter resources</span><input type="search" placeholder="Filter by name or folder path…" value={filter} onChange={(event) => setFilter(event.target.value)} /></label><span className="muted">Automatically refreshed</span></div>
    {loading && !resources ? <p role="status" className="empty">Loading resources…</p> : !resources ? <Empty title="Resources unavailable">Resolve the connection error above to load your resources.</Empty> : !visible.length ? <Empty title={filter ? "No matching resources" : "Connect your knowledge"}>{filter ? "Try a different name or path." : "Add a local folder or Outlook mail resource. Nothing is indexed until you run a scan."}</Empty> : <div className="resource-list">{visible.map((resource) => <ResourceRow key={resource.id} resource={resource} status={status} refresh={refresh} />)}</div>}
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

function CreateOutlookResource({ onCreated }: { onCreated: () => void }) {
  const stores = useQuery<Items<OutlookStore>>("/outlook/stores");
  const [storeId, setStoreId] = useState("");
  return <><p className="warning">Requires classic Outlook and the Outlook Python extra in the backend&apos;s Windows user session. Mail headers and bodies are indexed; attachment contents and automatic deletion sync are not supported. Remote models receive indexed mail excerpts when used.</p>
    <Feedback error={stores.error} retry={stores.refresh} />
    {stores.loading ? <p role="status">Connecting to classic Outlook…</p> : <label>Mailbox<select value={storeId} onChange={(event) => setStoreId(event.target.value)}><option value="">Select a mailbox</option>{stores.data?.items.map((store) => <option value={store.id} key={store.id}>{store.name}</option>)}</select></label>}
    {stores.data?.items.length === 0 && <p>No Outlook stores are available. Open classic Outlook and configure a profile, then retry.</p>}
    {storeId && <OutlookFolderForm key={storeId} storeId={storeId} onCreated={onCreated} />}
  </>;
}

function OutlookFolderForm({ storeId, onCreated }: { storeId: string; onCreated: () => void }) {
  const [parents, setParents] = useState<OutlookFolder[]>([]);
  const [selected, setSelected] = useState<OutlookFolder[]>([]);
  const parent = parents.at(-1);
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected.length) return;
    const form = new FormData(event.currentTarget);
    const result = await action.run<Operation>("/resources", {
      name: String(form.get("name")).trim(),
      connector_kind: "outlook_local",
      config: { store_id: storeId, folder_ids: selected.map((folder) => folder.id), include_subfolders: form.get("recursive") === "on" },
      description: selected.map((folder) => folder.path).join("; ").slice(0, 1000),
      verify: true, scan: false,
    });
    if (result) onCreated();
  }
  return <form className="form-grid" onSubmit={submit}>
    <label className="full">Resource name<input required name="name" maxLength={200} disabled={action.busy} placeholder="Work mail" /></label>
    <div className="full"><p>{parent?.path || "Mailbox root"}</p><button type="button" disabled={!parents.length || action.busy} onClick={() => setParents((current) => current.slice(0, -1))}>Up one folder</button>
      <OutlookFolderChoices key={parent?.id || "root"} storeId={storeId} parentId={parent?.id} selected={selected} busy={action.busy} toggle={(folder) => setSelected((current) => current.some((item) => item.id === folder.id) ? current.filter((item) => item.id !== folder.id) : [...current, folder])} browse={(folder) => setParents((current) => [...current, folder])} />
      <p className="muted">Selected: {selected.length ? selected.map((folder) => folder.path).join("; ") : "No folders selected"}</p>
      {selected.map((folder) => <button type="button" key={folder.id} disabled={action.busy} onClick={() => setSelected((current) => current.filter((item) => item.id !== folder.id))}>Remove {folder.name}</button>)}
      <label className="check"><input type="checkbox" name="recursive" disabled={action.busy} />Include subfolders of selected folders</label>
      <Feedback error={action.error} /><p className="muted">Deleting this resource removes Shovel&apos;s index, never Outlook mail. Removing a message from Outlook does not automatically erase its indexed copy.</p>
      <button className="primary" disabled={action.busy || !selected.length}>{action.busy ? "Adding mail resource…" : "Add and verify"}</button>
    </div>
  </form>;
}

function OutlookFolderChoices({ storeId, parentId, selected, busy, toggle, browse }: {
  storeId: string; parentId?: string; selected: OutlookFolder[]; busy: boolean;
  toggle: (folder: OutlookFolder) => void; browse: (folder: OutlookFolder) => void;
}) {
  const folders = useQuery<Items<OutlookFolder>>(`/outlook/folders?store_id=${segment(storeId)}${parentId ? `&parent_id=${segment(parentId)}` : ""}`);
  return <div><Feedback error={folders.error} retry={folders.refresh} />{folders.loading ? <p role="status">Loading folders…</p> : folders.data?.items.map((folder) => <div className="button-row" key={folder.id}>
    <label className="check"><input type="checkbox" checked={selected.some((item) => item.id === folder.id)} disabled={busy || Boolean(folders.error) || (selected.length >= 100 && !selected.some((item) => item.id === folder.id))} onChange={() => toggle(folder)} />{folder.name}</label>
    <button type="button" disabled={busy || Boolean(folders.error)} onClick={() => browse(folder)}>Browse {folder.name}</button>
  </div>)}</div>;
}

function ResourceRow({ resource, status, refresh }: { resource: Resource; status?: Status; refresh: () => void }) {
  const [scanOpen, setScanOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const action = useAction();
  const base = `/resources/${segment(resource.id)}`;
  const outlook = resource.connector_kind === "outlook_local";
  async function perform(path: string, method = "POST") {
    const operation = await action.run<Operation>(path, method === "DELETE" ? undefined : {}, method);
    if (operation) { action.setNotice(`${operation.kind.replaceAll("_", " ")} queued. Follow it in Background tasks.`); setConfirmDelete(false); refresh(); }
  }
  return <article className="resource-row">
    <div className="resource-main"><span className="folder-icon" aria-hidden="true">{outlook ? "✉" : "▱"}</span><div className="resource-info"><div className="title-line"><h3>{resource.name}</h3><Badge state={resource.state} /></div><p className="path">{outlook ? `Classic Outlook · ${resource.config.folder_ids?.length || 0} selected folders` : resource.config.root_path || "No root path provided"}</p>{resource.description && <p>{resource.description}</p>}{outlook && <p className="warning">Read-only mail indexing. Attachment contents and automatic deletion sync are not supported.</p>}<p className="resource-meta">{resource.connector_kind} · Last verified {date(resource.last_verified_at)}{resource.authority && ` · ${resource.authority}`}{resource.sensitivity && ` · ${resource.sensitivity}`}</p>{resource.state_reason && <p className="reason">{resource.state_reason}</p>}</div><div className="row-actions"><button onClick={() => setScanOpen(!scanOpen)} disabled={action.busy || ["paused", "revoked"].includes(resource.state)} aria-expanded={scanOpen}>{outlook ? "Scan mail" : "Scan folder"}</button><details className="action-menu"><summary>Manage<span className="sr-only"> {resource.name}</span></summary><div><button disabled={action.busy} onClick={() => perform(`${base}/verify`)}>Verify access</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/${resource.state === "paused" ? "resume" : "pause"}`)}>{resource.state === "paused" ? "Resume" : "Pause"}</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/revoke`)}>Revoke access</button><button className="danger" disabled={action.busy} onClick={() => setConfirmDelete(true)}>Delete resource…</button></div></details></div></div>
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
      <option value="incremental">Incremental — changed documents</option><option value="full_sweep">{resource.connector_kind === "outlook_local" ? "Full sweep — no deletion sync" : "Full sweep — reconcile folder"}</option><option value="targeted">Targeted — selected identifiers</option>
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
