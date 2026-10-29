"use client";

import { useState } from "react";
import { segment, useAction, useQuery, type Items, type Memory, type Operation, type Status } from "./api";
import { MCPManagement } from "./mcp";
import { Empty, Feedback } from "./ui";

const warmupNames: Record<string, string> = {
  index: "Knowledge index",
  memory_embedding: "Memory embedding",
  rag_embedding: "RAG embedding",
  chat_model: "Chat model",
};

export function Settings({ status }: { status?: Status }) {
  const [memoryOpen, setMemoryOpen] = useState(false);
  const chatReady = status?.model_configured && status.memory_configured;
  const providers = [
    { name: "Chat model", section: "model", ready: status?.model_configured, value: status?.model_configured ? status.model_name : "Required for chat", description: "Generates replies through the main Agent.", optional: false },
    { name: "Knowledge embedding", section: "embedding", ready: status?.embedding_configured, value: status?.embedding_configured ? "Vector search available" : "Keyword search available", description: "Adds semantic search to your local documents.", optional: true },
    { name: "Native Memory", section: "memory", ready: status?.memory_configured, value: status?.memory_configured ? "SQLite · local source of truth" : "Unavailable", description: "Long-term facts with a separate, rebuildable Zvec semantic index.", optional: false },
  ];
  return <div className="settings-content">
    <nav className="settings-index" aria-label="Settings sections">
      <a href="#settings-providers">Providers</a><a href="#settings-performance">Performance</a><a href="#settings-memory">Memory</a><a href="#settings-mcp">MCP servers</a>
    </nav>
    <section className="settings-card" id="settings-providers" aria-labelledby="settings-providers-heading" tabIndex={-1}>
      <header className="settings-card-header">
        <div><p className="settings-kicker">Models & connections</p><h2 id="settings-providers-heading">Runtime configuration</h2><p className="muted">Your configured providers and conversation readiness.</p></div>
        <span className={`badge ${status ? chatReady ? "good" : "bad" : ""}`}>{status ? chatReady ? "Configured for chat" : "Chat setup incomplete" : "Checking runtime"}</span>
      </header>
      {status ? <div className="provider-grid">{providers.map((provider) => <article className="provider-card" key={provider.section}>
        <div className="provider-card-heading"><h3>{provider.name}</h3><span className={`badge ${provider.ready ? "good" : provider.optional ? "" : "bad"}`}>{provider.ready ? "Configured" : provider.optional ? "Optional" : "Not configured"}</span></div>
        <code>[{provider.section}]</code><p className="provider-value">{provider.value}</p><p className="muted">{provider.description}</p>
      </article>)}</div> : <p className="muted" role="status">Waiting for runtime configuration…</p>}
      <p className="settings-note">To change providers, edit <code>settings.toml</code> and restart the backend. Model credentials are not shown here.</p>
    </section>
    <section className="settings-card" id="settings-performance" aria-labelledby="settings-performance-heading" tabIndex={-1}>
      <header className="settings-card-header"><div><p className="settings-kicker">Response health</p><h2 id="settings-performance-heading">Performance & warmup</h2><p className="muted">See what is initialized before your next conversation.</p></div></header>
      {status?.chat_capacity && <div className="chat-capacity"><p><strong>{status.chat_capacity.running}</strong> / {status.chat_capacity.limit} conversations running</p><span className="badge">{status.chat_capacity.waiting} waiting</span><p className="muted">Background Memory starts after foreground conversations are idle.</p></div>}
      {status?.warmup && <><h3>Startup warmup</h3><ul className="warmup-list">{Object.entries(status.warmup).map(([name, item]) => <li key={name}>
        <div><span className="warmup-name">{warmupNames[name] || name.replaceAll("_", " ")}</span>{item.elapsed_ms !== undefined && <span className="muted">{Math.round(item.elapsed_ms)} ms</span>}</div>
        <span className={`badge ${item.state === "ready" ? "good" : item.state === "failed" ? "bad" : ""}`}>{item.state}</span>
        {item.error_type && <p className="warning">{item.error_type} — check backend logs.</p>}
      </li>)}</ul><p className="muted">Local model warmup uses a synthetic prompt, not session data. Disabled components initialize on demand; ready models may later be unloaded.</p></>}
      {!status?.warmup && !status?.chat_capacity && <p className="muted">{status ? "Performance metrics are not available from this Host." : "Waiting for runtime status…"}</p>}
    </section>
    <section className="settings-card" id="settings-memory" aria-labelledby="settings-memory-heading" tabIndex={-1}>
      <header className="settings-card-header">
        <div><p className="settings-kicker">User context</p><h2 id="settings-memory-heading">Manage long-term Memory</h2><p className="muted">Review the facts your Agent remembers across sessions.</p></div>
        <button disabled={!status?.memory_configured} onClick={() => setMemoryOpen(!memoryOpen)} aria-expanded={memoryOpen} aria-controls="saved-memory">{memoryOpen ? "Hide saved Memory" : "Load saved Memory"}</button>
      </header>
      <p className="muted">Saved facts are read locally from SQLite. Semantic recall uses your configured <code>[memory.embedder]</code>; extraction uses <code>[memory.llm]</code> or the main model.</p>
      {status?.memory && <p role="status">Semantic index: <strong>{status.memory.index_state}</strong> · {status.memory.pending_index_operations} pending{status.memory.last_error && <span className="warning"> · {status.memory.last_error}</span>}</p>}
      <details className="settings-details"><summary>How Memory works</summary><p>Conversation history and session summaries are separate from long-term facts. Native Memory extracts durable user assertions, consolidates duplicates and preserves superseded facts.</p><p>Deleting a session removes its summary and session-scoped Memory, not cross-session facts. Forgetting a fact hides it immediately; vector cleanup is retried in the background. This does not erase source messages or backups.</p></details>
      <div id="saved-memory" hidden={!memoryOpen}>{memoryOpen && <Memories indexEnabled={status?.memory?.index_state !== "disabled"} />}</div>
    </section>
    <MCPManagement />
  </div>;
}

function Memories({ indexEnabled }: { indexEnabled: boolean }) {
  const memories = useQuery<Items<Memory>>("/memories");
  const [confirmId, setConfirmId] = useState("");
  const action = useAction();
  async function rebuild() {
    const result = await action.run<Operation>("/memory/index/rebuild");
    if (result) action.setNotice(`Index rebuild queued: ${result.id}. Track it in Background tasks.`);
  }
  async function forget(id: string) {
    const result = await action.run<{ deleted: boolean }>(`/memories/${segment(id)}`, undefined, "DELETE");
    if (result?.deleted) { setConfirmId(""); memories.refresh(); action.setNotice("Memory forgotten."); }
  }
  return <div className="memory-panel"><button disabled={action.busy || !indexEnabled} onClick={rebuild}>Rebuild semantic index</button><Feedback error={memories.error} retry={memories.refresh} /><Feedback error={action.error} notice={action.notice} />{memories.loading ? <p role="status">Loading saved Memory…</p> : memories.data?.items.length === 0 ? <Empty title="No saved facts">Long-term facts extracted from your messages will appear here.</Empty> : memories.data?.items.map((item) => <article className="memory-row" key={item.id}><div><p className="preserve">{item.memory}</p><p className="muted">{[item.kind, item.scope, item.state].filter(Boolean).join(" · ")}</p></div>{confirmId === item.id ? <div className="confirmation"><p>Forget this saved fact? Its vector will also be cleaned up.</p><div className="button-row"><button className="danger" disabled={action.busy} onClick={() => forget(item.id)}>Confirm forget</button><button disabled={action.busy} onClick={() => setConfirmId("")}>Keep fact</button></div></div> : <button disabled={action.busy} onClick={() => setConfirmId(item.id)}>Forget…</button>}</article>)}</div>;
}
