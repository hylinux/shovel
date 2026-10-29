"use client";

import { useReducer, useState, type FormEvent } from "react";
import Link from "next/link";
import { date, segment, useAction, useQuery, useSessionHistory, type Items, type Resource, type Session, type Status } from "./api";
import { Resources } from "./resources";
import { Tasks } from "./tasks";
import { Knowledge } from "./knowledge";
import { SearchDocuments } from "./search";
import { Settings } from "./settings";
import { Feedback } from "./ui";

const views = ["Resources", "Background tasks", "Search Documents", "Chat"] as const;
type View = typeof views[number] | "Settings";
const descriptions: Record<View, string> = {
  Resources: "Choose which local documents and Outlook mail Shovel can work with.",
  "Background tasks": "Follow scans, inspect events, and recover failed operations.",
  "Search Documents": "Find relevant passages in your indexed documents.",
  Chat: "Ask your documents. Keep the sources in view.",
  Settings: "Review providers, manage MCP servers and saved Memory.",
};

export default function Home() {
  const [view, setView] = useState<View>("Resources");
  const [sessionsOpen, setSessionsOpen] = useState(true);
  const [selectedSession, setSelectedSession] = useState("");
  const [sessionRevision, refreshSessions] = useReducer((value: number) => value + 1, 0);
  const status = useQuery<Status>("/status", 10000);
  const resourcesNeeded = view === "Resources" || view === "Background tasks" || view === "Search Documents";
  const resources = useQuery<Items<Resource>>(resourcesNeeded ? "/resources" : null, 2000);
  const showSessions = sessionsOpen;
  function navigate(item: View) {
    if (item === "Chat") {
      setSessionsOpen(view === "Chat" ? !sessionsOpen : true);
    }
    setView(item);
  }
  function selectSession(identifier: string) {
    setSelectedSession(identifier);
    setView("Chat");
  }
  return (
    <div className="workbench">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="Shovel home"><span className="brand-mark" aria-hidden="true">S</span>Shovel</Link>
        <p className="sidebar-caption">Personal Agent Platform</p>
        <nav className="main-navigation" aria-label="Main navigation">
          {views.map((item, index) => <button key={item} aria-current={view === item ? "page" : undefined} aria-expanded={item === "Chat" ? showSessions : undefined} aria-controls={item === "Chat" ? "session-navigation" : undefined} onClick={() => navigate(item)}><span aria-hidden="true" className="nav-symbol">{["▤", "≡", "⌕", "◇"][index]}</span>{item}{item === "Chat" && <span className="nav-chevron" aria-hidden="true">{showSessions ? "⌄" : "›"}</span>}</button>)}
        </nav>
        <section id="session-navigation" className="session-navigation" aria-label="Chat sessions" hidden={!showSessions}>
          <SessionNavigation selected={selectedSession} active={view === "Chat"} revision={sessionRevision} onSelect={selectSession} onDeleted={() => { setSelectedSession(""); refreshSessions(); }} />
        </section>
        <div className="sidebar-bottom">
          <nav className="settings-navigation" aria-label="Settings navigation"><button aria-current={view === "Settings" ? "page" : undefined} onClick={() => navigate("Settings")}><span className="nav-symbol" aria-hidden="true">⚙</span>Settings</button></nav>
          <div className="sidebar-footer"><span className={`connection-dot ${status.data && !status.error ? "connected" : ""}`} />{status.data && !status.error ? "Local runtime connected" : status.loading ? "Connecting to runtime…" : "Runtime unavailable"}<p>Your files. Your model choices.</p></div>
        </div>
      </aside>
      <main id="main" className="main" tabIndex={-1}>
        <header className="page-heading"><div><h1>{view}</h1><p>{descriptions[view]}</p></div><span className="local-label">Local-first runtime</span></header>
        <Feedback error={status.error} retry={status.refresh} />
        {resourcesNeeded && <Feedback error={resources.error} retry={resources.refresh} />}
        {view === "Resources" && <Resources resources={resources.data?.items} loading={resources.loading} status={status.data} refresh={resources.refresh} />}
        {view === "Background tasks" && <Tasks resources={resources.data?.items || []} />}
        {view === "Search Documents" && <SearchDocuments resources={resources.data?.items || []} status={status.data} />}
        {view === "Chat" && <Knowledge status={status.data} sessionId={selectedSession} onSessionChanged={refreshSessions} />}
        {view === "Settings" && <Settings status={status.data} />}
        <footer className="page-footer">Shovel · Local documents, connected knowledge</footer>
      </main>
    </div>
  );
}

function SessionNavigation({ selected, active, revision, onSelect, onDeleted }: { selected: string; active: boolean; revision: number; onSelect: (identifier: string) => void; onDeleted: () => void }) {
  const history = useSessionHistory(revision);
  const action = useAction();
  async function create() {
    const session = await action.run<Session>("/conversations", {});
    if (session) { history.refresh(); onSelect(session.id); }
  }
  return <>
    <button className="new-session" disabled={action.busy} onClick={create}><span aria-hidden="true">+</span>{action.busy ? "Creating…" : "New Session"}</button>
    <Feedback error={action.error} />
    <div className="session-history" role="region" aria-label="Session history" tabIndex={0} aria-busy={history.loading}>
      <Feedback error={history.error} retry={history.refresh} />
      {history.loading && <p className="muted" role="status">Loading sessions…</p>}
      {!history.loading && !history.error && history.data.length === 0 && <p className="muted">No sessions yet.</p>}
      <ul className="session-items">{history.data.map((session) => <li key={session.id}>
        <button className="session-button" aria-current={active && selected === session.id ? "true" : undefined} onClick={() => onSelect(session.id)} title={session.title || "Untitled session"}>
          <span className="session-marker" aria-hidden="true" />
          <span className="session-copy"><span className="session-title">{session.title || "Untitled session"}</span><span className="session-time" title={date(session.updated_at)}>{date(session.updated_at)}</span></span>
        </button>
        {selected === session.id && <SessionMaintenance identifier={session.id} refresh={history.refresh} deleted={onDeleted} />}
      </li>)}</ul>
      {history.hasMore && <button className="load-sessions" disabled={history.loading} onClick={history.loadMore}>Load more sessions</button>}
    </div>
  </>;
}

function SessionMaintenance({ identifier, refresh, deleted }: { identifier: string; refresh: () => void; deleted: () => void }) {
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
  return <details className="session-actions"><summary>Manage session</summary>
    <form onSubmit={rename}><label>New title<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} disabled={action.busy} /></label><button disabled={action.busy}>Rename</button></form>
    <button disabled={action.busy} onClick={() => setConfirm(true)}>Delete session…</button>
    {confirm && <div role="alert" className="confirmation"><p>Delete this session, its messages and short-term Memory? Long-term Memory is kept.</p>
      <button disabled={action.busy} onClick={remove}>Confirm deletion</button><button onClick={() => setConfirm(false)}>Keep session</button></div>}
    <Feedback error={action.error} notice={action.notice} />
  </details>;
}
