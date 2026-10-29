"use client";

import { useState } from "react";
import { segment, useAction, useQuery, type Items, type Memory, type Status } from "./api";
import { Empty, Feedback } from "./ui";

export function Settings({ status }: { status?: Status }) {
  const [memoryOpen, setMemoryOpen] = useState(false);
  return <section>
    <section className="configuration"><h2>Runtime configuration</h2>
      <p>Configure providers in <code>settings.toml</code>, then restart the backend. Credentials stay outside this interface.</p>
      {status ? <ul>
        <li><code>[model]</code> — {status.model_configured ? `ready (${status.model_name})` : "required for chat"}</li>
        <li><code>[embedding]</code> — {status.embedding_configured ? "vector indexing available" : "optional; keyword indexing and search still work"}</li>
        <li><code>[memory]</code> — {status.memory_configured ? "ready; enabled for every session" : "required for chat; configure a Memory embedder and extraction model"}</li>
      </ul> : <p className="muted">Waiting for runtime configuration…</p>}
      <p className="muted">Chat uses Memory by default. Short-term Memory stores user messages without another LLM extraction; long-term Memory extracts and merges user facts through mem0.</p>
    </section>
    <section className="memory-section"><h2>Manage long-term Memory</h2>
      <p className="muted">View or forget long-term facts stored by mem0. Deleting a session clears its short-term Memory, not its long-term facts. Forgetting a fact does not erase its short-term copy. Loading this list contacts your configured Memory provider.</p>
      <button className="text-button" disabled={!status?.memory_configured} onClick={() => setMemoryOpen(!memoryOpen)} aria-expanded={memoryOpen}>{memoryOpen ? "Hide saved Memory" : "Load saved Memory"}</button>
      {memoryOpen && <Memories />}
    </section>
  </section>;
}

function Memories() {
  const memories = useQuery<Items<Memory>>("/memories");
  const [confirmId, setConfirmId] = useState("");
  const action = useAction();
  async function forget(id: string) {
    const result = await action.run<{ deleted: boolean }>(`/memories/${segment(id)}`, undefined, "DELETE");
    if (result?.deleted) { setConfirmId(""); memories.refresh(); action.setNotice("Memory forgotten."); }
  }
  return <div className="memory-panel"><Feedback error={memories.error} retry={memories.refresh} /><Feedback error={action.error} notice={action.notice} />{memories.loading ? <p role="status">Loading saved Memory…</p> : memories.data?.items.length === 0 ? <Empty title="No saved facts">Long-term facts extracted from your messages will appear here.</Empty> : memories.data?.items.map((item) => <article className="memory-row" key={item.id}><p className="preserve">{item.memory}</p>{confirmId === item.id ? <div className="confirmation"><p>Forget this saved fact? This cannot be undone.</p><div className="button-row"><button className="danger" disabled={action.busy} onClick={() => forget(item.id)}>Confirm forget</button><button disabled={action.busy} onClick={() => setConfirmId("")}>Keep fact</button></div></div> : <button disabled={action.busy} onClick={() => setConfirmId(item.id)}>Forget…</button>}</article>)}</div>;
}
