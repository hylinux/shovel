"use client";

import { useI18n } from "./locale-context";

import { useEffect, useReducer, useRef, useState, type FormEvent, type KeyboardEvent, type PointerEvent } from "react";
import Link from "next/link";
import { api, errorText, segment, useAction, useQuery, useSessionHistory, type Items, type MessagePage, type Resource, type Session, type Status } from "./api";
import { resolveInitialSession, sessionCacheKey } from "./session-startup";
import { Resources } from "./resources";
import { Tasks } from "./tasks";
import { Knowledge } from "./knowledge";
import { SearchDocuments } from "./search";
import { Settings } from "./settings";
import { Feedback } from "./ui";
import { ProactiveServices } from "./proactive";
import { ApprovalCenter, ExecutionActivity } from "./platform-activity";
import { CapabilityLibrary } from "./capabilities";

const platformViews = ["Proactive Services", "Approval Center", "Activity", "Skills", "Workflows"] as const;
type View = "Resources" | "Background tasks" | "Search Documents" | "Chat" | typeof platformViews[number] | "Settings";
const navigationGroups = [
  { id: "resource-navigation", label: "Resource Management", symbol: "▤", items: [
    { view: "Resources", symbol: "▤" },
    { view: "Background tasks", symbol: "≡" },
    { view: "Search Documents", symbol: "⌕" },
  ] },
  { id: "development-navigation", label: "Development", symbol: "⌘", items: [
    { view: "Skills", symbol: "▧" },
    { view: "Workflows", symbol: "⌘" },
  ] },
  { id: "service-navigation", label: "Services", symbol: "◷", items: [
    { view: "Proactive Services", symbol: "◷" },
    { view: "Approval Center", symbol: "✓" },
    { view: "Activity", symbol: "↗" },
  ] },
] as const;
const descriptions: Record<View, string> = {
  Resources: "Choose which local documents, Outlook mail and public websites Shovel can work with.",
  "Background tasks": "Follow scans, inspect events, and recover failed operations.",
  "Search Documents": "Find relevant passages in your indexed documents.",
  Chat: "Ask your documents. Keep the sources in view.",
  Settings: "Review providers, manage MCP servers and saved Memory.",
  "Proactive Services": "Configure automation without putting triggers inside capabilities.",
  "Approval Center": "Review exact actions and provide execution-bound input.",
  Activity: "Inspect durable executions, frozen versions and decisions.",
  Skills: "Create isolated Skill drafts and explicitly publish immutable versions.",
  Workflows: "Design trigger-agnostic workflows with the V1 node palette.",
};

export default function Home() {
  const { t } = useI18n();
  const [view, setView] = useState<View>("Chat");
  const [visited, setVisited] = useState<Set<string>>(new Set());
  const [sessionsOpen, setSessionsOpen] = useState(true);
  const [selectedSession, setSelectedSession] = useState("");
  const [sessionRevision, refreshSessions] = useReducer((value: number) => value + 1, 0);
  const initialSession = useRef<Promise<string> | null>(null);
  const currentSession = useRef("");
  const sessionWorkspace = useRef("");
  const [creatingSession, setCreatingSession] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [creationRevision, retryCreation] = useReducer((value: number) => value + 1, 0);
  async function restoreInitialSession() {
    const workspace = (await api<Status>("/status")).workspace_id;
    sessionWorkspace.current = workspace;
    if (currentSession.current) sessionStorage.setItem(sessionCacheKey(workspace), currentSession.current);
    const identifier = await resolveInitialSession(
      workspace, sessionStorage,
      async (id) => { await api<MessagePage>(`/conversations/${segment(id)}/messages?limit=1`); },
      async () => (await api<Session>("/conversations", { method: "POST", body: "{}" })).id,
    );
    if (currentSession.current) sessionStorage.setItem(sessionCacheKey(workspace), currentSession.current);
    return identifier;
  }
  useEffect(() => {
    let active = true;
    // Keep one mutation across Strict Mode's effect replay; only the active subscriber updates the UI.
    initialSession.current ??= restoreInitialSession();
    initialSession.current.then((identifier) => {
      if (!active) return;
      if (!currentSession.current) {
        currentSession.current = identifier;
        setSelectedSession(identifier);
      }
      refreshSessions();
      setCreatingSession(false);
    }, (error: unknown) => {
      if (!active) return;
      setSessionError(errorText(error));
      setCreatingSession(false);
    });
    return () => { active = false; };
  }, [creationRevision]);
  function retryInitialSession() {
    initialSession.current = null;
    setSessionError("");
    setCreatingSession(true);
    retryCreation();
  }
  const status = useQuery<Status>("/status", 10000);
  const resourcesNeeded = view === "Background tasks" || view === "Search Documents";
  const resources = useQuery<Items<Resource>>(resourcesNeeded ? "/resources" : null, 2000);
  const showSessions = sessionsOpen;
  function navigate(item: View) {
    if (item === "Chat") {
      setSessionsOpen(view === "Chat" ? !sessionsOpen : true);
    }
    setView(item);
    setVisited((current) => new Set(current).add(item));
  }
  function selectSession(identifier: string) {
    setSessionError("");
    currentSession.current = identifier;
    persistSession(identifier);
    setSelectedSession(identifier);
    setView("Chat");
  }
  function persistSession(identifier: string) {
    if (sessionWorkspace.current) {
      try {
        const key = sessionCacheKey(sessionWorkspace.current);
        if (identifier) sessionStorage.setItem(key, identifier);
        else sessionStorage.removeItem(key);
      }
      catch (error) { setSessionError(`Cannot retain this Session across refresh: ${errorText(error)}`); }
    }
  }
  function deleteSelectedSession() {
    persistSession("");
    currentSession.current = "";
    setSelectedSession("");
    refreshSessions();
  }
  const connectionLabel = status.data && !status.error ? t("Local runtime connected") : status.loading ? t("Connecting to runtime…") : t("Runtime unavailable");
  return (
    <div className="workbench">
      <title>{t("Shovel — local knowledge workbench")}</title>
      <a className="skip-link" href="#main">{t("Skip to content")}</a>
      <aside id="sidebar" className="sidebar">
        <Link href="/" className="brand" aria-label={t("Shovel home")}><span className="brand-mark" aria-hidden="true">{t("S")}</span>{t("Shovel")}</Link>
        <div className="sidebar-caption">
          <span className="sidebar-caption-label" title={t("Personal Agent Platform")}>{t("Personal Agent Platform")}</span>
          <div className="sidebar-connection" title={connectionLabel}><span className={`connection-dot ${status.data && !status.error ? "connected" : ""}`} aria-hidden="true" /><span className="connection-label">{connectionLabel}</span></div>
        </div>
        <nav className="main-navigation" aria-label={t("Main navigation")}>
          {navigationGroups.map((group) => <SidebarGroup key={group.id} group={group} view={view} onNavigate={navigate} />)}
          <div className="chat-navigation" data-expanded={showSessions}>
            <button className="navigation-button" aria-current={view === "Chat" ? "page" : undefined} aria-expanded={showSessions} aria-controls="session-navigation" onClick={() => navigate("Chat")}><span aria-hidden="true" className="nav-symbol">◇</span><span className="navigation-label">{t("Chat")}</span><span className="nav-chevron" aria-hidden="true">{showSessions ? "⌄" : "›"}</span></button>
            <section id="session-navigation" className="session-navigation" aria-label={t("Chat sessions")} hidden={!showSessions}>
              <SessionNavigation selected={selectedSession} active={view === "Chat"} initializing={creatingSession} revision={sessionRevision} onSelect={selectSession} onDeleted={deleteSelectedSession} />
            </section>
          </div>
        </nav>
        <div className="sidebar-bottom">
          <nav className="settings-navigation" aria-label={t("Settings navigation")}><button aria-current={view === "Settings" ? "page" : undefined} onClick={() => navigate("Settings")}><span className="nav-symbol" aria-hidden="true">⚙</span>{t("Settings")}</button></nav>
        </div>
        <SidebarResizeHandle />
      </aside>
      <main id="main" className={view === "Chat" ? "main main-chat" : "main"} tabIndex={-1}>
        <header className="page-heading"><div><h1>{t(view)}</h1><p title={view === "Chat" ? t(descriptions[view]) : undefined}>{t(descriptions[view])}</p></div>{view !== "Chat" && <span className="local-label">{t("Local-first runtime")}</span>}</header>
        <Feedback error={status.error} retry={status.refresh} />
        {resourcesNeeded && <Feedback error={resources.error} retry={resources.refresh} />}
        {view === "Resources" && <Resources status={status.data} refresh={resources.refresh} />}
        {view === "Background tasks" && <Tasks resources={resources.data?.items || []} />}
        {view === "Search Documents" && <SearchDocuments resources={resources.data?.items || []} status={status.data} />}
        {view === "Chat" && <><Feedback error={sessionError} retry={retryInitialSession} />{creatingSession && !selectedSession ? <p role="status">{t("Opening session…")}</p> : <Knowledge status={status.data} sessionId={selectedSession} onSessionChanged={refreshSessions} />}</>}
        {view === "Settings" && <Settings status={status.data} />}
        {platformViews.filter((item) => visited.has(item)).map((item) => <div key={item} className="platform-view" hidden={view !== item}>
          {item === "Proactive Services" && <ProactiveServices active={view === item} />}
          {item === "Approval Center" && <ApprovalCenter active={view === item} />}
          {item === "Activity" && <ExecutionActivity active={view === item} />}
          {item === "Skills" && <CapabilityLibrary kind="skill" active={view === item} />}
          {item === "Workflows" && <CapabilityLibrary kind="workflow" active={view === item} />}
        </div>)}
        {view !== "Chat" && <footer className="page-footer">{t("Shovel · Local documents, connected knowledge")}</footer>}
      </main>
    </div>
  );
}

function SidebarResizeHandle() {
  const { t } = useI18n();
  const handle = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const [resizing, setResizing] = useState(false);
  const [dimensions, setDimensions] = useState({ width: 395, maximum: 420 });
  function maximumWidth() {
    return Math.max(200, Math.min(420, Math.round(window.innerWidth * .4)));
  }
  useEffect(() => {
    const sidebar = handle.current?.parentElement;
    const workbench = sidebar?.parentElement;
    if (!sidebar || !workbench) return;
    const observer = new ResizeObserver(() => {
      const width = Math.round(sidebar.getBoundingClientRect().width);
      const maximum = maximumWidth();
      setDimensions((current) => current.width === width && current.maximum === maximum ? current : { width, maximum });
    });
    observer.observe(sidebar);
    observer.observe(workbench);
    return () => observer.disconnect();
  }, []);
  function resize(width: number) {
    handle.current?.parentElement?.parentElement?.style.setProperty("--sidebar-width", `${Math.max(200, Math.min(maximumWidth(), width))}px`);
  }
  function reset() {
    handle.current?.parentElement?.parentElement?.style.removeProperty("--sidebar-width");
  }
  function start(event: PointerEvent<HTMLDivElement>) {
    if (!event.isPrimary || event.button !== 0 || drag.current) return;
    const sidebar = event.currentTarget.parentElement;
    if (!sidebar) return;
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointerId: event.pointerId, x: event.clientX, width: sidebar.getBoundingClientRect().width };
    setResizing(true);
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId === event.pointerId) resize(drag.current.width + event.clientX - drag.current.x);
  }
  function finish(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return;
    event.preventDefault();
    const width = event.currentTarget.parentElement?.getBoundingClientRect().width ?? dimensions.width;
    if (event.key === "Enter") reset();
    else resize(event.key === "Home" ? 200 : event.key === "End" ? maximumWidth() : width + (event.key === "ArrowRight" ? 10 : -10));
  }
  return <div ref={handle} className="sidebar-resizer" role="separator" tabIndex={0} aria-label={t("Resize sidebar")} aria-controls="sidebar" aria-orientation="vertical" aria-valuemin={200} aria-valuemax={dimensions.maximum} aria-valuenow={dimensions.width} data-resizing={resizing}
    title={t("Drag to resize. Arrow keys adjust; double-click or Enter resets.")}
    onPointerDown={start} onPointerMove={move} onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish} onDoubleClick={reset} onKeyDown={keyDown} />;
}

function SidebarGroup({ group, view, onNavigate }: { group: typeof navigationGroups[number]; view: View; onNavigate: (view: View) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return <div className="navigation-group" data-active={group.items.some((item) => item.view === view)}>
    <button className="navigation-button navigation-group-toggle" aria-expanded={open} aria-controls={group.id} onClick={() => setOpen((current) => !current)}><span className="nav-symbol" aria-hidden="true">{group.symbol}</span><span className="navigation-label">{t(group.label)}</span><span className="nav-chevron" aria-hidden="true">{open ? "⌄" : "›"}</span></button>
    <div id={group.id} className="navigation-children" role="group" aria-label={t("{p0} pages", { p0: t(group.label) })} hidden={!open}>
      {group.items.map((item) => <button className="navigation-button" key={item.view} aria-current={view === item.view ? "page" : undefined} onClick={() => onNavigate(item.view)}><span className="nav-symbol" aria-hidden="true">{item.symbol}</span><span className="navigation-label">{t(item.view)}</span></button>)}
    </div>
  </div>;
}

function SessionNavigation({ selected, active, initializing, revision, onSelect, onDeleted }: { selected: string; active: boolean; initializing: boolean; revision: number; onSelect: (identifier: string) => void; onDeleted: () => void }) {
  const { t, date } = useI18n();
  const history = useSessionHistory(revision);
  const action = useAction();
  async function create() {
    const session = await action.run<Session>("/conversations", {});
    if (session) { history.refresh(); onSelect(session.id); }
  }
  return <>
    <button className="new-session" disabled={action.busy || initializing} onClick={create}><span aria-hidden="true">+</span>{action.busy ? t("Creating…") : initializing ? t("Opening…") : t("New Session")}</button>
    <Feedback error={action.error} />
    <div className="session-history" role="region" aria-label={t("Session history")} tabIndex={0} aria-busy={history.loading}>
      <Feedback error={history.error} retry={history.refresh} />
      {history.loading && <p className="muted" role="status">{t("Loading sessions…")}</p>}
      {!history.loading && !history.error && history.data.length === 0 && <p className="muted">{t("No sessions yet.")}</p>}
      <ul className="session-items">{history.data.map((session) => <li key={session.id}>
        <button className="session-button" aria-current={active && selected === session.id ? "true" : undefined} onClick={() => onSelect(session.id)} title={session.title || t("Untitled session")}>
          <span className="session-marker" aria-hidden="true" />
          <span className="session-copy"><span className="session-title">{session.title || t("Untitled session")}</span><span className="session-time" title={date(session.updated_at)}>{date(session.updated_at)}</span></span>
        </button>
        {selected === session.id && <SessionMaintenance identifier={session.id} refresh={history.refresh} deleted={onDeleted} />}
      </li>)}</ul>
      {history.hasMore && <button className="load-sessions" disabled={history.loading} onClick={history.loadMore}>{t("Load more sessions")}</button>}
    </div>
  </>;
}

function SessionMaintenance({ identifier, refresh, deleted }: { identifier: string; refresh: () => void; deleted: () => void }) {
  const { t } = useI18n();
  const action = useAction();
  const [title, setTitle] = useState("");
  const [confirm, setConfirm] = useState(false);
  async function rename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (await action.run(`/conversations/${segment(identifier)}`, { title }, "PATCH")) { refresh(); action.setNotice("Session renamed."); }
  }
  async function remove() {
    if (await action.run(`/conversations/${segment(identifier)}`, undefined, "DELETE")) deleted();
  }
  return <details className="session-actions"><summary>{t("Manage session")}</summary>
    <form onSubmit={rename}><label>{t("New title")}<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} disabled={action.busy} /></label><button disabled={action.busy}>{t("Rename")}</button></form>
    <button disabled={action.busy} onClick={() => setConfirm(true)}>{t("Delete session…")}</button>
    {confirm && <div role="alert" className="confirmation"><p>{t("Delete this session, its messages and short-term Memory? Long-term Memory is kept.")}</p>
      <button disabled={action.busy} onClick={remove}>{t("Confirm deletion")}</button><button onClick={() => setConfirm(false)}>{t("Keep session")}</button></div>}
    <Feedback error={action.error} notice={action.notice} />
  </details>;
}
