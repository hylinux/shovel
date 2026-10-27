"use client";

import { useState, type FormEvent } from "react";
import { segment, useAction, useQuery, type Citation, type Conversation, type Items, type Memory, type Message, type MessagePage, type Resource, type Status } from "./api";
import { Empty, Feedback, Sources } from "./ui";

export function Knowledge({ resources, status }: { resources: Resource[]; status?: Status }) {
  const [tab, setTab] = useState<"chat" | "search">("chat");
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = useState("auto");
  const [memory, setMemory] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const available = resources.filter((resource) => !["paused", "revoked"].includes(resource.state));
  const resourceIds = selected.filter((id) => available.some((resource) => resource.id === id));
  return <section>
    <div className="subnav" aria-label="Knowledge tools"><button aria-pressed={tab === "chat"} onClick={() => setTab("chat")}>Conversation</button><button aria-pressed={tab === "search"} onClick={() => setTab("search")}>Search documents</button></div>
    <details className="scope-panel"><summary>Knowledge scope <span>{resourceIds.length ? `${resourceIds.length} selected` : "All accessible resources"} · {mode} search</span></summary><p className="muted">Leave every resource unchecked to search all accessible resources.</p><div className="resource-checks">{available.length ? available.map((resource) => <label className="check" key={resource.id}><input type="checkbox" checked={selected.includes(resource.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, resource.id] : current.filter((id) => id !== resource.id))} />{resource.name}</label>) : <p>No accessible resources yet. Add and scan a folder in Resources.</p>}</div><label className="mode-field">Search mode<select value={mode} onChange={(event) => setMode(event.target.value)}><option value="auto">Auto</option><option value="keyword">Keyword</option><option value="vector" disabled={!status?.embedding_configured}>Vector</option><option value="hybrid" disabled={!status?.embedding_configured}>Hybrid</option></select></label></details>
    {tab === "chat" ? <><div className="memory-choice"><label className="check"><input type="checkbox" checked={memory} disabled={!status?.memory_configured} onChange={(event) => setMemory(event.target.checked)} />Use Memory for messages in this session</label><p>Off by default. When enabled, your statements may be sent to configured mem0 models and persisted as facts.{!status?.memory_configured && " Configure [memory] in settings.toml to enable it."}</p></div><Conversations status={status} resourceIds={resourceIds} mode={mode} memory={memory && Boolean(status?.memory_configured)} /></> : <Search resourceIds={resourceIds} mode={mode} />}
    <section className="memory-section"><button className="text-button" onClick={() => setMemoryOpen(!memoryOpen)} aria-expanded={memoryOpen}>{memoryOpen ? "Hide" : "Manage"} saved Memory</button><p className="muted">View or forget facts stored by mem0. Opening this section contacts your configured Memory provider.</p>{memoryOpen && <Memories />}</section>
  </section>;
}

function Conversations({ status, resourceIds, mode, memory }: { status?: Status; resourceIds: string[]; mode: string; memory: boolean }) {
  const [offset, setOffset] = useState(0);
  const conversations = useQuery<Items<Conversation>>(`/conversations?limit=100&offset=${offset}`);
  const [selected, setSelected] = useState("");
  const action = useAction();
  async function create() {
    const result = await action.run<Conversation>("/conversations", {});
    if (result) { conversations.refresh(); setSelected(result.id); }
  }
  return <div className="chat-layout"><aside className="conversation-list">
    <button className="primary" disabled={action.busy} onClick={create}>{action.busy ? "Creating…" : "+ New conversation"}</button>
    <Feedback error={action.error || conversations.error} retry={conversations.error ? conversations.refresh : undefined} />
    {conversations.loading ? <p role="status">Loading conversations…</p> : conversations.data?.items.length === 0 ? <p className="muted">No conversations yet.</p> : conversations.data?.items.map((conversation) =>
      <button key={conversation.id} className="conversation-button" aria-current={selected === conversation.id ? "true" : undefined} onClick={() => setSelected(conversation.id)}>{conversation.title || "Untitled conversation"}</button>)}
    <div className="button-row"><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button>
      <button disabled={(conversations.data?.items.length ?? 0) < 100} onClick={() => setOffset(offset + 100)}>Next</button></div>
  </aside><div className="conversation-content">{selected ? <>
    <ConversationMaintenance key={`manage-${selected}`} identifier={selected} refresh={conversations.refresh} deleted={() => { setSelected(""); conversations.refresh(); }} />
    <Chat key={selected} conversationId={selected} status={status} resourceIds={resourceIds} mode={mode} memory={memory} onSent={conversations.refresh} />
  </> : <Empty title="A conversation grounded in your documents">Create a conversation to ask a question. Or use Search documents to inspect your index without a chat model.</Empty>}</div></div>;
}

function ConversationMaintenance({ identifier, refresh, deleted }: { identifier: string; refresh: () => void; deleted: () => void }) {
  const action = useAction();
  const [title, setTitle] = useState("");
  const [confirm, setConfirm] = useState(false);
  async function rename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (await action.run(`/conversations/${segment(identifier)}`, { title }, "PATCH")) { refresh(); action.setNotice("Conversation renamed."); }
  }
  async function remove() {
    if (await action.run(`/conversations/${segment(identifier)}`, undefined, "DELETE")) deleted();
  }
  return <details><summary>Manage conversation</summary><form onSubmit={rename} className="button-row">
    <label>New title<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} disabled={action.busy} /></label>
    <button disabled={action.busy}>Rename</button></form>
    <button disabled={action.busy} onClick={() => setConfirm(true)}>Delete conversation…</button>
    {confirm && <div role="alert" className="confirmation"><p>Delete this conversation and all its messages? This does not forget saved Memory.</p>
      <button disabled={action.busy} onClick={remove}>Confirm deletion</button><button onClick={() => setConfirm(false)}>Keep conversation</button></div>}
    <Feedback error={action.error} notice={action.notice} /></details>;
}

function Chat({ conversationId, status, resourceIds, mode, memory, onSent }: { conversationId: string; status?: Status; resourceIds: string[]; mode: string; memory: boolean; onSent: () => void }) {
  const [before, setBefore] = useState<number>();
  const history = useQuery<MessagePage>(`/conversations/${segment(conversationId)}/messages?limit=100${before ? `&before=${before}` : ""}`);
  const [content, setContent] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!content.trim() || action.busy) return;
    const question = content.trim();
    setPendingQuestion(question);
    const response = await action.run<Message>(`/conversations/${segment(conversationId)}/messages`, { content: question, resource_ids: resourceIds, memory_enabled: memory, search_mode: mode });
    if (response) { setContent(""); setBefore(undefined); onSent(); }
    setPendingQuestion("");
    // Failed model calls may still have persisted user and failed assistant messages.
    history.refresh();
  }
  return <><div className="button-row">
    <button disabled={!history.data?.next_before || action.busy} onClick={() => setBefore(history.data?.next_before ?? undefined)}>Older messages</button>
    {before && <button onClick={() => setBefore(undefined)}>Latest messages</button>}
  </div><div className="messages" aria-label="Conversation messages" aria-busy={history.loading || action.busy}>
    <Feedback error={history.error} retry={history.refresh} />
    {history.loading ? <p role="status">Loading conversation…</p> : history.data?.items.length === 0 ? <Empty title="What would you like to find out?">Ask a specific question about your indexed documents. Answers include source excerpts when available.</Empty> : history.data?.items.map((message) =>
      <article className={`message ${message.role}`} key={message.id}><h3>{message.role === "user" ? "You" : "Shovel"}{message.state === "failed" && <span className="badge bad">Failed</span>}</h3>
        <p className="preserve">{message.content || (message.state === "failed" ? "The model could not complete this response." : "")}</p>
        {message.warnings?.map((warning, index) => <p className="warning" key={index}>{warning}</p>)}<Sources items={message.citations || []} /></article>)}
    {action.busy && <div role="status" className="pending-answer"><p className="preserve">{pendingQuestion}</p><span>Shovel is preparing an answer…</span></div>}
  </div><form className="composer" onSubmit={submit}>
    {!status?.model_configured && <p className="warning">Configure <code>[model]</code> in settings.toml for chat. Search documents is available without a model.</p>}
    <Feedback error={action.error} /><label htmlFor="question">Message</label>
    <textarea id="question" value={content} onChange={(event) => setContent(event.target.value)} placeholder="Ask about your documents…" rows={3} required disabled={action.busy || !status?.model_configured} />
    <div className="composer-footer"><span className="muted">{memory ? "Memory enabled for this message" : "Memory off"} · {resourceIds.length ? `${resourceIds.length} resources` : "All accessible resources"}</span>
      <button className="primary" disabled={action.busy || !content.trim() || !status?.model_configured || history.loading || Boolean(history.error)}>{action.busy ? "Waiting for model…" : "Send message"}</button></div>
  </form></>;
}

function Search({ resourceIds, mode }: { resourceIds: string[]; mode: string }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{ mode: string; items: Citation[] }>();
  const action = useAction();
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const response = await action.run<{ mode: string; items: Citation[] }>("/search", { query: query.trim(), resource_ids: resourceIds, mode, limit: 6 });
    if (response) setResult(response);
  }
  return <div className="search-tool"><h2>Search documents</h2><p className="muted">Test retrieval directly. No chat model or Memory is used.</p><form onSubmit={submit} className="search-form"><label className="grow">Search query<input value={query} onChange={(event) => setQuery(event.target.value)} required placeholder="Find a phrase, concept, or document…" disabled={action.busy} /></label><button className="primary" disabled={action.busy || !query.trim()}>{action.busy ? "Searching…" : "Search index"}</button></form><Feedback error={action.error} />{result && <><p className="muted">{result.items.length} results · {result.mode} search</p>{result.items.length ? <Sources items={result.items} /> : <Empty title="No matching passages">Try a different query, expand the resource scope, or check that a scan has completed.</Empty>}</>}</div>;
}

function Memories() {
  const memories = useQuery<Items<Memory>>("/memories");
  const [confirmId, setConfirmId] = useState("");
  const action = useAction();
  async function forget(id: string) {
    const result = await action.run<{ deleted: boolean }>(`/memories/${segment(id)}`, undefined, "DELETE");
    if (result?.deleted) { setConfirmId(""); memories.refresh(); action.setNotice("Memory forgotten."); }
  }
  return <div className="memory-panel"><Feedback error={memories.error} retry={memories.refresh} /><Feedback error={action.error} notice={action.notice} />{memories.loading ? <p role="status">Loading saved Memory…</p> : memories.data?.items.length === 0 ? <Empty title="No saved facts">Memory stays off until you explicitly enable it for chat.</Empty> : memories.data?.items.map((item) => <article className="memory-row" key={item.id}><p className="preserve">{item.memory}</p>{confirmId === item.id ? <div className="confirmation"><p>Forget this saved fact? This cannot be undone.</p><div className="button-row"><button className="danger" disabled={action.busy} onClick={() => forget(item.id)}>Confirm forget</button><button disabled={action.busy} onClick={() => setConfirmId("")}>Keep fact</button></div></div> : <button disabled={action.busy} onClick={() => setConfirmId(item.id)}>Forget…</button>}</article>)}</div>;
}
