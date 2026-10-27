"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, type Items, type Resource, type Status } from "./api";
import { Resources } from "./resources";
import { Tasks } from "./tasks";
import { Knowledge } from "./knowledge";
import { Feedback } from "./ui";

const views = ["Resources", "Background tasks", "Knowledge chat"] as const;
type View = typeof views[number];

export default function Home() {
  const [view, setView] = useState<View>("Resources");
  const status = useQuery<Status>("/status", 10000);
  const resources = useQuery<Items<Resource>>("/resources", 2000);
  return (
    <div className="workbench">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="Shovel home"><span className="brand-mark" aria-hidden="true">S</span>Shovel</Link>
        <p className="sidebar-caption">Personal knowledge workbench</p>
        <nav aria-label="Main navigation">
          {views.map((item, index) => <button key={item} aria-current={view === item ? "page" : undefined} onClick={() => setView(item)}><span aria-hidden="true" className="nav-symbol">{["▤", "≡", "◇"][index]}</span>{item}</button>)}
        </nav>
        <div className="sidebar-footer"><span className={`connection-dot ${status.data && !status.error ? "connected" : ""}`} />{status.data && !status.error ? "Local runtime connected" : status.loading ? "Connecting to runtime…" : "Runtime unavailable"}<p>Your files. Your model choices.</p></div>
      </aside>
      <main id="main" className="main" tabIndex={-1}>
        <header className="page-heading"><div><h1>{view}</h1><p>{view === "Resources" ? "Choose which local documents and Outlook mail Shovel can work with." : view === "Background tasks" ? "Follow scans, inspect events, and recover failed operations." : "Ask your documents. Keep the sources in view."}</p></div><span className="local-label">Local-first runtime</span></header>
        <Feedback error={status.error} retry={status.refresh} />
        {status.data && <details className="configuration"><summary>Runtime configuration <span>{status.data.model_configured ? status.data.model_name || "Model ready" : "Chat model not configured"}</span></summary><p>Configure providers in <code>settings.toml</code>, then restart the backend. Credentials stay outside this interface.</p><ul><li><code>[model]</code> — {status.data.model_configured ? `ready (${status.data.model_name})` : "required for conversational answers"}</li><li><code>[embedding]</code> — {status.data.embedding_configured ? "vector indexing available" : "optional; keyword indexing and search still work"}</li><li><code>[memory]</code> — {status.data.memory_configured ? "available, always opt-in" : "optional; mem0 is not configured"}</li></ul></details>}
        <Feedback error={resources.error} retry={resources.refresh} />
        {view === "Resources" && <Resources resources={resources.data?.items} loading={resources.loading} status={status.data} refresh={resources.refresh} />}
        {view === "Background tasks" && <Tasks resources={resources.data?.items || []} />}
        {view === "Knowledge chat" && <Knowledge resources={resources.data?.items || []} status={status.data} />}
        <footer className="page-footer">Shovel · Local documents, connected knowledge</footer>
      </main>
    </div>
  );
}
