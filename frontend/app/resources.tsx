"use client";

import { useI18n } from "./locale-context";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { type Connector, type Items, type Operation, type OutlookFolder, type OutlookStore, type Resource, type Status, active, segment, useAction, useQuery } from "./api";
import { usePageQuery } from "./pagination-query";
import { Badge, Empty, Feedback, Pagination } from "./ui";
import { Profiles } from "./profiles";

export function Resources({ status, refresh, onTasks }: { status?: Status; refresh: () => void; onTasks: () => void }) {
  const { t } = useI18n();
  const connectors = useQuery<Items<Connector>>("/connectors");
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");
  const [creations, setCreations] = useState<{ operation: Operation; name: string }[]>([]);
  const page = usePageQuery<Resource>(`/resources?q=${segment(filter)}`, 2000);
  const resources = page.data?.items;
  const loading = page.loading;
  const refreshPage = page.refresh;
  const refreshResources = useCallback(() => { refreshPage(); refresh(); }, [refreshPage, refresh]);
  function onQueued(operation: Operation, name: string) {
    setCreations((current) => [...current.filter((item) => item.operation.id !== operation.id), { operation, name }]);
    refreshResources();
    setAdding(false);
  }
  const [kind, setKind] = useState("local_fs");
  const connector = connectors.data?.items.find((connector) => connector.kind === kind);
  const visible = resources || [];
  return <section>
    <div className="section-toolbar"><div><h2>{t("Connected resources ")}<span className="count">{page.data?.total ?? "—"}</span></h2><p>{t("Choose local documents, classic Outlook mail or a public website, verify access, then build its search index.")}</p></div><button className="primary" onClick={() => setAdding(!adding)} aria-expanded={adding}>{adding ? t("Close form") : t("+ Add resource")}</button></div>
    {adding && <div className="inline-panel"><h3>{t("Add a resource")}</h3><p className="muted">{t("Resource creation is queued. Running scans finish before creation and verification start.")}</p><Feedback error={connectors.error} retry={connectors.refresh} />{connectors.loading ? <p role="status">{t("Loading connectors…")}</p> : <><label>{t("Resource type")}<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="local_fs">{t("Local folder")}</option><option value="outlook_local">{t("Classic Outlook mail (Windows)")}</option><option value="web_search">{t("Web Search — public website")}</option></select></label>{!connector ? <p>{t("This resource type is not available from this runtime.")}</p> : <><p>{connector.description}</p>{kind === "outlook_local" ? <CreateOutlookResource onQueued={onQueued} /> : kind === "web_search" ? <CreateWebResource onQueued={onQueued} /> : <CreateResource onQueued={onQueued} />}</>}</>}</div>}
    {creations.map(({ operation, name }) => <ResourceCreationNotice key={operation.id} operation={operation} name={name} onFinished={refreshResources} onTasks={onTasks} onDismiss={() => setCreations((current) => current.filter((item) => item.operation.id !== operation.id))} />)}
    <div className="list-toolbar"><label className="search-field"><span className="sr-only">{t("Filter resources")}</span><input type="search" placeholder={t("Filter by name, folder path or website…")} maxLength={2000} value={filter} onChange={(event) => setFilter(event.target.value)} /></label><span className="muted">{t("Automatically refreshed")}</span></div>
    <Feedback error={page.error} retry={page.refresh} />
    {loading && !resources ? <p role="status" className="empty">{t("Loading resources…")}</p> : !resources ? <Empty title={t("Resources unavailable")}>{t("Resolve the connection error above to load your resources.")}</Empty> : !visible.length ? <Empty title={filter ? t("No matching resources") : t("Connect your knowledge")}>{filter ? t("Try a different name, path or website.") : t("Add a local folder, Outlook mail or public website resource. Nothing is indexed until you run a scan.")}</Empty> : <div className="resource-list">{visible.map((resource) => <ResourceRow key={resource.id} resource={resource} status={status} refresh={refreshResources} />)}</div>}
    {page.data && <Pagination label={t("Resources")} pagination={page} onPage={page.setPage} onPageSize={page.setPageSize} />}
  </section>;
}

function pollCreation(operation: Operation) { return active(operation.state); }

export function ResourceCreationNotice({ operation, name, onFinished, onTasks, onDismiss }: {
  operation: Operation; name: string; onFinished: () => void; onTasks: () => void; onDismiss: () => void;
}) {
  const { t } = useI18n();
  const query = useQuery<Operation>(`/operations/${segment(operation.id)}`, 2000, pollCreation);
  const current = query.data || operation;
  const finished = ["succeeded", "completed", "completed_with_errors", "failed", "cancelled"].includes(current.state);
  const reported = useRef(false);
  useEffect(() => {
    if (finished && !reported.current) { reported.current = true; onFinished(); }
  }, [finished, onFinished]);
  const problem = ["completed_with_errors", "failed", "cancelled"].includes(current.state);
  const message = current.state === "queued" ? "Resource creation queued. It will start after running scans finish."
    : current.state === "running" ? "Creating and verifying the resource. It may not be listed yet."
    : current.state === "succeeded" || current.state === "completed" ? "Resource creation completed. If it is not visible, check the filter and pagination."
    : current.state === "completed_with_errors" ? "Resource creation finished with errors. Check Background tasks for details."
    : current.state === "failed" ? "Resource creation failed. Check Background tasks before submitting again."
    : current.state === "cancelled" ? "Resource creation was cancelled. Check Background tasks before submitting again."
    : "Resource creation status is not recognized. Check Background tasks.";
  const detail = current.error || (typeof current.result.message === "string" ? current.result.message : "");
  return <article className="inline-panel">
    <div className="title-line"><h3>{name}</h3><Badge state={current.state} /></div>
    <p className={problem ? "feedback error" : finished ? "feedback success" : "muted"} role={problem ? "alert" : "status"}>{t(message)}</p>
    {detail && <p className="reason">{t(detail)}</p>}
    <p className="path">{t("Creation task")}: {current.id}</p>
    <Feedback error={query.error} retry={query.refresh} />
    <div className="button-row"><button onClick={onTasks}>{t("View in Background tasks")}</button>{finished && <button onClick={onDismiss}>{t("Dismiss notification")}</button>}</div>
  </article>;
}

type CreationProps = { onQueued: (operation: Operation, name: string) => void };

function CreateResource({ onQueued }: CreationProps) {
  const { t } = useI18n();
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const result = await action.run<Operation>("/resources", { name: String(form.get("name")).trim(), connector_kind: "local_fs", config: { root_path: String(form.get("root_path")).trim() }, description: String(form.get("description")).trim() || undefined, verify: true, scan: false });
    if (result) onQueued(result, String(form.get("name")).trim());
  }
  return <form onSubmit={submit} className="form-grid"><label>{t("Name")}<input name="name" required maxLength={200} placeholder={t("Research notes")} autoFocus disabled={action.busy} /></label><label>{t("Folder path")}<input name="root_path" required placeholder={"C:\\Users\\you\\Documents"} className="path" disabled={action.busy} /></label><label className="full">{t("Description ")}<span className="muted">{t("(optional)")}</span><input name="description" placeholder={t("What is in this folder?")} disabled={action.busy} /></label><div className="full"><Feedback error={action.error} /><p className="muted">{t("Creation verifies folder access. Scanning is a separate, explicit action.")}</p><button className="primary" disabled={action.busy}>{action.busy ? t("Adding folder…") : t("Add and verify")}</button></div></form>;
}

function CreateOutlookResource({ onQueued }: CreationProps) {
  const { t } = useI18n();
  const stores = useQuery<Items<OutlookStore>>("/outlook/stores");
  const [storeId, setStoreId] = useState("");
  return <><p className="warning">{t("Requires classic Outlook and the Outlook Python extra in the backend's Windows user session. Mail headers and bodies are indexed; attachment contents and automatic deletion sync are not supported. Remote models receive indexed mail excerpts when used.")}</p>
    <Feedback error={stores.error} retry={stores.refresh} />
    {stores.loading ? <p role="status">{t("Connecting to classic Outlook…")}</p> : <label>{t("Mailbox")}<select value={storeId} onChange={(event) => setStoreId(event.target.value)}><option value="">{t("Select a mailbox")}</option>{stores.data?.items.map((store) => <option value={store.id} key={store.id}>{store.name}</option>)}</select></label>}
    {stores.data?.items.length === 0 && <p>{t("No Outlook stores are available. Open classic Outlook and configure a profile, then retry.")}</p>}
    {storeId && <OutlookFolderForm key={storeId} storeId={storeId} onQueued={onQueued} />}
  </>;
}

function CreateWebResource({ onQueued }: CreationProps) {
  const { t } = useI18n();
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const result = await action.run<Operation>("/resources", {
      name: String(form.get("name")).trim(), connector_kind: "web_search",
      config: {
        start_url: String(form.get("start_url")).trim(),
        max_pages: Number(form.get("max_pages")), max_depth: Number(form.get("max_depth")),
        request_delay_seconds: Number(form.get("request_delay_seconds")),
        max_attempts: Number(form.get("max_attempts")),
        max_total_bytes: Number(form.get("max_total_mb")) * 1024 * 1024,
        max_duration_seconds: Number(form.get("max_duration_minutes")) * 60,
        include_query_urls: form.get("include_query_urls") === "on",
      },
      description: String(form.get("description")).trim() || undefined, verify: true, scan: false,
    });
    if (result) onQueued(result, String(form.get("name")).trim());
  }
  return <form onSubmit={submit} className="form-grid">
    <label>{t("Name")}<input name="name" required maxLength={200} placeholder={t("Product documentation")} autoFocus disabled={action.busy} /></label>
    <label>{t("Website URL")}<input type="url" name="start_url" required maxLength={4096} placeholder="https://example.com/docs/" disabled={action.busy} /></label>
    <label className="full">{t("Description ")}<span className="muted">{t("(optional)")}</span><input name="description" disabled={action.busy} /></label>
    <label>{t("Maximum pages")}<input type="number" name="max_pages" min="1" max="10000" step="1" defaultValue="1000" required disabled={action.busy} /></label>
    <label>{t("Maximum link depth")}<input type="number" name="max_depth" min="0" max="30" step="1" defaultValue="10" required disabled={action.busy} /></label>
    <label>{t("Request delay (seconds)")}<input type="number" name="request_delay_seconds" min="1" max="60" step="0.5" defaultValue="1" required disabled={action.busy} /></label>
    <label>{t("Maximum attempts per request")}<input type="number" name="max_attempts" min="1" max="5" step="1" defaultValue="3" required disabled={action.busy} /></label>
    <label>{t("Download limit (MiB)")}<input type="number" name="max_total_mb" min="1" max="1024" step="1" defaultValue="100" required disabled={action.busy} /></label>
    <label>{t("Time limit (minutes)")}<input type="number" name="max_duration_minutes" min="1" max="1440" step="1" defaultValue="60" required disabled={action.busy} /></label>
    <div className="full"><label className="check"><input type="checkbox" name="include_query_urls" disabled={action.busy} />{t("Include query URLs (may create duplicate pages or crawl traps)")}</label>
      <p className="warning">{t("Only crawl sites you are permitted to index. Crawling stays on the same origin. An HTML page URL follows links within its containing directory; a directory URL limits crawling to that subtree. robots.txt, noindex/nofollow and slower site-requested rates are respected. Private networks, logins and JavaScript rendering are not supported.")}</p>
      <p className="muted">{t("Links and Sitemaps discover pages. Limits and failures are reported in Background tasks; a scan cannot guarantee complete website coverage. Creation verifies access; use Crawl website afterwards to index text and generate embeddings.")}</p>
      <Feedback error={action.error} /><button className="primary" disabled={action.busy}>{action.busy ? t("Adding website…") : t("Add and verify")}</button>
    </div>
  </form>;
}

function OutlookFolderForm({ storeId, onQueued }: CreationProps & { storeId: string }) {
  const { t } = useI18n();
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
    if (result) onQueued(result, String(form.get("name")).trim());
  }
  return <form className="form-grid" onSubmit={submit}>
    <label className="full">{t("Resource name")}<input required name="name" maxLength={200} disabled={action.busy} placeholder={t("Work mail")} /></label>
    <div className="full"><p>{parent?.path || t("Mailbox root")}</p><button type="button" disabled={!parents.length || action.busy} onClick={() => setParents((current) => current.slice(0, -1))}>{t("Up one folder")}</button>
      <OutlookFolderChoices key={parent?.id || "root"} storeId={storeId} parentId={parent?.id} selected={selected} busy={action.busy} toggle={(folder) => setSelected((current) => current.some((item) => item.id === folder.id) ? current.filter((item) => item.id !== folder.id) : [...current, folder])} browse={(folder) => setParents((current) => [...current, folder])} />
      <p className="muted">{t("Selected: ")}{selected.length ? selected.map((folder) => folder.path).join("; ") : t("No folders selected")}</p>
      {selected.map((folder) => <button type="button" key={folder.id} disabled={action.busy} onClick={() => setSelected((current) => current.filter((item) => item.id !== folder.id))}>{t("Remove ")}{folder.name}</button>)}
      <label className="check"><input type="checkbox" name="recursive" disabled={action.busy} />{t("Include subfolders of selected folders")}</label>
      <Feedback error={action.error} /><p className="muted">{t("Deleting this resource removes Shovel's index, never Outlook mail. Removing a message from Outlook does not automatically erase its indexed copy.")}</p>
      <button className="primary" disabled={action.busy || !selected.length}>{action.busy ? t("Adding mail resource…") : t("Add and verify")}</button>
    </div>
  </form>;
}

function OutlookFolderChoices({ storeId, parentId, selected, busy, toggle, browse }: {
  storeId: string; parentId?: string; selected: OutlookFolder[]; busy: boolean;
  toggle: (folder: OutlookFolder) => void; browse: (folder: OutlookFolder) => void;
}) {
  const { t } = useI18n();
  const folders = useQuery<Items<OutlookFolder>>(`/outlook/folders?store_id=${segment(storeId)}${parentId ? `&parent_id=${segment(parentId)}` : ""}`);
  return <div><Feedback error={folders.error} retry={folders.refresh} />{folders.loading ? <p role="status">{t("Loading folders…")}</p> : folders.data?.items.map((folder) => <div className="button-row" key={folder.id}>
    <label className="check"><input type="checkbox" checked={selected.some((item) => item.id === folder.id)} disabled={busy || Boolean(folders.error) || (selected.length >= 100 && !selected.some((item) => item.id === folder.id))} onChange={() => toggle(folder)} />{folder.name}</label>
    <button type="button" disabled={busy || Boolean(folders.error)} onClick={() => browse(folder)}>{t("Browse ")}{folder.name}</button>
  </div>)}</div>;
}

function ResourceRow({ resource, status, refresh }: { resource: Resource; status?: Status; refresh: () => void }) {
  const { t, date } = useI18n();
  const [scanOpen, setScanOpen] = useState(false);
  const [profilesOpen, setProfilesOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const action = useAction();
  const base = `/resources/${segment(resource.id)}`;
  const outlook = resource.connector_kind === "outlook_local";
  const web = resource.connector_kind === "web_search";
  async function perform(path: string, method = "POST") {
    const operation = await action.run<Operation>(path, method === "DELETE" ? undefined : {}, method);
    if (operation) { action.setNotice(`${operation.kind.replaceAll("_", " ")} queued. Follow it in Background tasks.`); setConfirmDelete(false); refresh(); }
  }
  return <article className="resource-row">
    <div className="resource-main"><span className="folder-icon" aria-hidden="true">{outlook ? "✉" : "▱"}</span><div className="resource-info"><div className="title-line"><h3>{resource.name}</h3><Badge state={resource.state} /></div><p className="path">{outlook ? t("Classic Outlook · {p0} selected folders", { p0: resource.config.folder_ids?.length || 0 }) : web ? resource.config.start_url || t("No website URL provided") : resource.config.root_path || t("No root path provided")}</p>{resource.description && <p>{resource.description}</p>}{outlook && <p className="warning">{t("Read-only mail indexing. Attachment contents and automatic deletion sync are not supported.")}</p>}{web && <p className="warning">{t("Public pages only · Up to ")}{resource.config.max_pages}{t(" pages / depth ")}{resource.config.max_depth}{t(" · At least ")}{resource.config.request_delay_seconds}{t("s between requests. Missing pages are not automatically deleted.")}</p>}<p className="resource-meta">{t(resource.connector_kind)}{t(" · Last verified ")}{date(resource.last_verified_at)}{resource.authority && ` · ${resource.authority}`}{resource.sensitivity && ` · ${t(resource.sensitivity)}`}</p>{resource.state_reason && <p className="reason">{t(resource.state_reason)}</p>}</div><div className="row-actions"><button onClick={() => setScanOpen(!scanOpen)} disabled={action.busy || ["paused", "revoked"].includes(resource.state)} aria-expanded={scanOpen}>{outlook ? t("Scan mail") : web ? t("Crawl website") : t("Scan folder")}</button><details className="action-menu"><summary>{t("Manage")}<span className="sr-only"> {resource.name}</span></summary><div><button disabled={action.busy} onClick={() => perform(`${base}/verify`)}>{t("Verify access")}</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/${resource.state === "paused" ? "resume" : "pause"}`)}>{resource.state === "paused" ? t("Resume") : t("Pause")}</button><button disabled={action.busy || resource.state === "revoked"} onClick={() => perform(`${base}/revoke`)}>{t("Revoke access")}</button><button className="danger" disabled={action.busy} onClick={() => setConfirmDelete(true)}>{t("Delete resource…")}</button></div></details></div></div>
    <Feedback error={action.error} notice={action.notice} />
    <button onClick={() => setProfilesOpen(!profilesOpen)} aria-expanded={profilesOpen}>{t("Manage scan profiles")}</button>
    {profilesOpen && <Profiles resourceId={resource.id} />}
    {confirmDelete && <div className="confirmation" role="alert"><p>{t("Delete ")}<strong>{resource.name}</strong>{t("? This removes the resource and its index, not your original files.")}</p><div className="button-row"><button className="danger" disabled={action.busy} onClick={() => perform(base, "DELETE")}>{action.busy ? t("Deleting…") : t("Delete resource")}</button><button disabled={action.busy} onClick={() => setConfirmDelete(false)}>{t("Keep resource")}</button></div></div>}
    {scanOpen && <ScanForm key={status?.embedding_configured ? "embed" : "keyword"} resource={resource} embeddingConfigured={Boolean(status?.embedding_configured)} onQueued={() => { refresh(); setScanOpen(false); action.setNotice("Scan queued. Follow its progress in Background tasks."); }} />}
  </article>;
}

function ScanForm({ resource, embeddingConfigured, onQueued }: { resource: Resource; embeddingConfigured: boolean; onQueued: () => void }) {
  const { t } = useI18n();
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
    <h4 className="full">{t("Scan ")}{resource.name}</h4>
    <label>{t("Profile")}<select value={profile} onChange={(event) => setProfile(event.target.value)} disabled={action.busy || profiles.loading}>
      {(profiles.data?.items || [{ name: "default", is_enabled: true }]).map((item) => <option key={item.name} value={item.name} disabled={!item.is_enabled}>{item.name}{!item.is_enabled && t(" (disabled)")}</option>)}
    </select></label>
    <Feedback error={profiles.error} retry={profiles.refresh} />
    <label>{t("Scan mode")}<select value={mode} onChange={(event) => setMode(event.target.value)} disabled={action.busy}>
      <option value="incremental">{t("Incremental — changed documents")}</option><option value="full_sweep">{["outlook_local", "web_search"].includes(resource.connector_kind) ? t("Full sweep — no deletion sync") : t("Full sweep — reconcile folder")}</option><option value="targeted">{t("Targeted — selected identifiers")}</option>
    </select></label>
    <label>{t("Document limit ")}<span className="muted">{t("(optional)")}</span><input type="number" name="limit" min="1" step="1" disabled={action.busy} /></label>
    {mode === "targeted" && <label className="full">{t("Target identifiers, one per line")}<textarea name="targets" required disabled={action.busy} /></label>}
    <div className="full"><label className="check"><input type="checkbox" checked={embed} onChange={(event) => setEmbed(event.target.checked)} disabled={!embeddingConfigured || action.busy} />{t("Generate embeddings")}</label>
      {resource.connector_kind === "web_search" && <p className="muted">{t("Each scan crawls the configured public URL scope again; unchanged content skips re-embedding. Document limit only limits indexing after discovery, not crawl requests. Target identifiers are website URLs.")}</p>}
      <p className="muted">{embeddingConfigured ? t("Turn off for a keyword-only scan. Keyword search works without vectors.") : t("No embedding provider configured. This scan builds a keyword index; vectors are not required.")}</p>
      <label className="check"><input type="checkbox" name="force" disabled={action.busy} />{t("Reprocess unchanged documents")}</label><Feedback error={action.error} />
      <button className="primary" disabled={action.busy || Boolean(profiles.error)}>{action.busy ? t("Queuing scan…") : t("Start scan")}</button>
    </div>
  </form>;
}
