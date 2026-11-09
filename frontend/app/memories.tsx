"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { active, segment, useAction, useQuery, type Items, type Memory, type MemoryInput, type Operation, type Status } from "./api";
import { memoryInput, memoryPayload } from "./memory-model";
import { useI18n } from "./locale-context";
import { Empty, Feedback } from "./ui";

const kinds = ["semantic", "episodic", "procedural"] as const;
const scopes = ["user", "workspace", "agent", "session"] as const;
const predicates = ["prefers", "dislikes", "uses", "works_at", "has_name", "has_role", "has_timezone", "has_language", "has_goal", "has_constraint", "decides", "knows_about"];
function pollOperation(operation: Operation) { return active(operation.state); }

export function MemoryManagement({ status, sessionId = "" }: { status?: Status; sessionId?: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return <section className="settings-card" id="settings-memory" aria-labelledby="settings-memory-heading" tabIndex={-1}>
    <header className="settings-card-header">
      <div><p className="settings-kicker">{t("User context")}</p><h2 id="settings-memory-heading">{t("Manage long-term Memory")}</h2><p className="muted">{t("Review the facts your Agent remembers across sessions.")}</p></div>
      <button disabled={!status?.memory_configured} onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="saved-memory">{open ? t("Hide saved Memory") : t("Load saved Memory")}</button>
    </header>
    <p className="muted">{t("Saved facts are read locally from SQLite. Semantic recall uses your configured ")}<code>[memory.embedder]</code>{t("; extraction uses ")}<code>[memory.llm]</code>{t(" or the main model.")}</p>
    {status?.memory && <p role="status">{t("Semantic index: ")}<strong>{t(status.memory.index_state)}</strong> · {status.memory.pending_index_operations}{t(" pending")}{status.memory.last_error && <span className="warning"> · {status.memory.last_error}</span>}</p>}
    <p>{t("Extraction review")}: <strong>{t(status?.memory ? status.memory.review_required ? "Required before active recall" : "Automatic activation (default)" : "Checking runtime")}</strong></p>
    <p className="muted">{t("To require review of newly extracted facts, set ")}<code>review_required = true</code>{t(" under ")}<code>[memory]</code>{t(" in settings.toml and restart the backend. The default is false; existing active facts are not changed.")}</p>
    <details className="settings-details"><summary>{t("How Memory works")}</summary>
      <p>{t("Session history and session summaries are separate from long-term facts. Native Memory extracts durable user assertions, consolidates duplicates and preserves superseded facts.")}</p>
      <p>{t("Extraction scores measure model confidence, not verified truth. Candidates are not active until approved; rejection also applies to active facts.")}</p>
      <p>{t("Deleting a session removes its summary and session-scoped Memory, not cross-session facts. Forgetting a fact hides it immediately; vector cleanup is retried in the background. This does not erase source messages or backups.")}</p>
    </details>
    <div id="saved-memory" hidden={!open}><Memories enabled={open && Boolean(status?.memory_configured)} indexEnabled={status?.memory?.index_state !== "disabled"} sessionId={sessionId} /></div>
  </section>;
}

function Memories({ enabled, indexEnabled, sessionId }: { enabled: boolean; indexEnabled: boolean; sessionId: string }) {
  const { t } = useI18n();
  const [history, setHistory] = useState(true);
  const [state, setState] = useState("");
  const [editor, setEditor] = useState<{ original?: Memory; draft: MemoryInput }>();
  const [confirm, setConfirm] = useState<{ item: Memory; decision: "approve" | "reject" | "forget" }>();
  const [operation, setOperation] = useState<Operation>();
  const [pending, setPending] = useState(false);
  const [localError, setLocalError] = useState("");
  const action = useAction();
  const sessionQuery = sessionId ? `&conversation_id=${segment(sessionId)}` : "";
  const path = enabled ? `/memories?include_history=${history}${sessionQuery}` : null;
  const memories = useQuery<Items<Memory>>(path, 5000);
  const currentData = memories.dataPath === path ? memories.data : undefined;
  const currentError = memories.errorPath === path ? memories.error : "";
  const waiting = Boolean(path && !currentData && !currentError);
  const rows = currentData?.items.filter(item => !state || item.state === state) ?? [];
  const busy = action.busy || pending;
  function watch(result: Operation | undefined) {
    if (result) { setOperation(result); setPending(active(result.state)); setConfirm(undefined); }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!editor || busy) return;
    let payload;
    try { payload = memoryPayload(editor.draft, editor.original); }
    catch (error) { setLocalError(error instanceof Error ? error.message : "Memory text is required."); return; }
    setLocalError("");
    const result = await action.run<Operation>(editor.original ? `/memories/${segment(editor.original.id)}` : "/memories", payload, editor.original ? "PUT" : "POST");
    if (result) { watch(result); setEditor(undefined); }
  }
  async function decide() {
    if (!confirm || busy) return;
    const { item, decision } = confirm;
    if (decision === "forget") {
      const result = await action.run<{ deleted: boolean }>(`/memories/${segment(item.id)}?conversation_id=${segment(sessionId)}`, undefined, "DELETE");
      if (result?.deleted) { setConfirm(undefined); memories.refresh(); action.setNotice("Memory forgotten."); }
    } else watch(await action.run<Operation>(`/memories/${segment(item.id)}/review`, { decision, conversation_id: sessionId }));
  }
  return <div className="memory-panel">
    <div className="button-row">
      <button disabled={busy} onClick={() => { setLocalError(""); setEditor({ draft: memoryInput(undefined, sessionId) }); }}>{t("Create Memory")}</button>
      <button disabled={busy || !indexEnabled} onClick={async () => watch(await action.run<Operation>("/memory/index/rebuild"))}>{t("Rebuild semantic index")}</button>
      <button disabled={action.busy} onClick={memories.refresh}>{t("Refresh")}</button>
      <label><input type="checkbox" checked={history} onChange={event => setHistory(event.target.checked)} />{t("Include history")}</label>
      <label>{t("State")}<select value={state} onChange={event => setState(event.target.value)}>
        <option value="">{t("All states")}</option>{["active", "candidate", "superseded", "expired", "deleted", "rejected"].map(value => <option key={value} value={value}>{t(value)}</option>)}
      </select></label>
    </div>
    {sessionId && <p className="muted">{t("Includes Memory available to the current Session.")}</p>}
    <Feedback error={currentError} retry={memories.refresh} /><Feedback error={action.error || localError} notice={action.notice} />
    {operation && <MemoryOperation key={operation.id} initial={operation} onSettled={() => { setPending(false); memories.refresh(); }} />}
    {editor && <form className="memory-editor" onSubmit={save}>
      <h3>{t(editor.original ? "Edit Memory (creates a replacement)" : "Create Memory")}</h3>
      <fieldset disabled={busy}>
        <label>{t("Memory text")}<textarea rows={3} maxLength={1500} required value={editor.draft.text} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, text: event.target.value } })} /></label>
        <div className="memory-fields">
          <label>{t("Kind")}<select disabled={Boolean(editor.original)} value={editor.draft.kind} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, kind: event.target.value as MemoryInput["kind"], occurred_at: event.target.value === "episodic" ? editor.draft.occurred_at : null, time_precision: event.target.value === "episodic" ? editor.draft.time_precision : undefined } })}>{kinds.map(value => <option key={value} value={value}>{t(value)}</option>)}</select></label>
          <label>{t("Scope")}<select disabled={Boolean(editor.original)} value={editor.draft.scope} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, scope: event.target.value as MemoryInput["scope"], session_id: event.target.value === "session" ? sessionId : "" } })}>{scopes.map(value => <option key={value} value={value} disabled={value === "session" && !sessionId}>{t(value)}</option>)}</select></label>
          <label>{t("Subject")}<input maxLength={200} required value={editor.draft.subject} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, subject: event.target.value } })} /></label>
          <label>{t("Predicate")}<select value={editor.draft.predicate} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, predicate: event.target.value } })}>{predicates.map(value => <option key={value} value={value}>{t(value)}</option>)}</select></label>
          <label>{t("Object")}<input maxLength={1000} value={editor.draft.object} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, object: event.target.value } })} /></label>
          <label>{t("Topic")}<input maxLength={200} value={editor.draft.topic} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, topic: event.target.value } })} /></label>
          <label>{t("Importance")}<input type="number" min={0} max={1} step="any" required value={editor.draft.importance} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, importance: Number(event.target.value) } })} /></label>
          {(["expires_at", ...(editor.draft.kind === "episodic" ? ["occurred_at" as const] : [])] as const).map(field => <label key={field}>{t(field === "expires_at" ? "Expires at (UTC Unix seconds)" : "Occurred at (UTC Unix seconds)")}<input type="number" min={0} step={1} value={editor.draft[field] ?? ""} onChange={event => setEditor({ ...editor, draft: { ...editor.draft, [field]: event.target.value ? Number(event.target.value) : null, ...(field === "occurred_at" ? { time_precision: undefined } : {}) } })} /></label>)}
        </div>
        {editor.draft.scope === "session" && <p>{t("Session")}: <code>{editor.draft.session_id}</code></p>}
        <p className="muted">{t("Kind and scope cannot change on edit. Saving queues an Operation; the list updates only after it succeeds.")}</p>
        <div className="button-row"><button className="primary" type="submit">{t("Save Memory")}</button><button type="button" onClick={() => setEditor(undefined)}>{t("Cancel")}</button></div>
      </fieldset>
    </form>}
    {waiting ? <p role="status">{t("Loading saved Memory…")}</p> : rows.length === 0 ? <Empty title={t("No saved facts")}>{t("Long-term facts extracted from your messages will appear here.")}</Empty> : rows.map(item => <article className="memory-row" key={item.id}>
      <p className="preserve">{item.memory}</p>
      <p className="muted">{[item.kind, item.scope, item.state].map(value => t(value)).join(" · ")}</p>
      <MemoryEvidence item={item} />
      {item.provenance?.some(source => source.source_kind === "user_settings") ? <p className="muted">{t("Managed in General settings. Edit or clear the profile field there.")}</p> : <div className="button-row">
        {item.state === "active" && <button disabled={busy} onClick={() => { setLocalError(""); setEditor({ original: item, draft: memoryInput(item, sessionId) }); }}>{t("Edit")}</button>}
        {item.state === "candidate" && <button disabled={busy} onClick={() => setConfirm({ item, decision: "approve" })}>{t("Approve candidate…")}</button>}
        {(item.state === "candidate" || item.state === "active") && <button disabled={busy} onClick={() => setConfirm({ item, decision: "reject" })}>{t("Reject…")}</button>}
        {item.state !== "deleted" && <button disabled={busy} onClick={() => setConfirm({ item, decision: "forget" })}>{t("Forget…")}</button>}
      </div>}
      {confirm?.item.id === item.id && <div className="confirmation">
        <p>{t(confirm.decision === "forget" ? "Forget this saved fact? Its vector will also be cleaned up." : confirm.decision === "approve" ? "Approve this candidate for active recall? Approval is not verification of truth." : "Reject this fact? The same rejected fact will not be automatically recreated.")}</p>
        <div className="button-row"><button className={confirm.decision === "approve" ? "primary" : "danger"} disabled={busy} onClick={decide}>{t(confirm.decision === "forget" ? "Confirm forget" : confirm.decision === "approve" ? "Confirm approve" : "Confirm reject")}</button><button disabled={busy} onClick={() => setConfirm(undefined)}>{t("Cancel")}</button></div>
      </div>}
    </article>)}
  </div>;
}

export function MemoryEvidence({ item }: { item: Memory }) {
  const { t, date } = useI18n();
  return <details className="settings-details"><summary>{t("Sources, evidence and history")}</summary>
    <dl className="memory-evidence">
      <dt>{t("Source")}</dt><dd>{t(item.source ?? "unknown")}</dd>
      <dt>{t("Extraction score (not verified truth)")}</dt><dd>{item.confidence ?? "—"}</dd>
      <dt>{t("Importance")}</dt><dd>{item.importance ?? "—"}</dd>
      <dt>{t("Revision")}</dt><dd>{item.revision ?? "—"}</dd>
      {item.superseded_by && <><dt>{t("Replaced by")}</dt><dd><code>{item.superseded_by}</code></dd></>}
      <dt>{t("Created")}</dt><dd>{date(item.created_at)}</dd>
      <dt>{t("Expires")}</dt><dd>{date(item.expires_at)}</dd>
      {item.kind === "episodic" && <><dt>{t("Occurred")}</dt><dd>{item.episode_time_source === "explicit" ? <>{date(item.occurred_at, item.episode_time_precision === "day" ? "day" : "instant")} · {t(item.episode_time_precision === "day" ? "Explicit event date (UTC; time unspecified)" : "Explicit episode time")}</> : t(item.episode_time_source === "legacy_unspecified" ? "Legacy episode time unspecified (not a known event date)" : "Recorded time fallback (not an asserted event time)")}</dd></>}
      {[["Subject", item.subject], ["Predicate", item.predicate], ["Object", item.object], ["Topic", item.topic], ["Session", item.session_id]].filter(([, value]) => value).map(([label, value]) => <div className="memory-evidence-pair" key={label}><dt>{t(label!)}</dt><dd className="preserve">{value}</dd></div>)}
    </dl>
    {item.provenance?.length ? item.provenance.map((source, index) => <blockquote className="memory-source" key={index}>
      <p className="preserve">{source.evidence}</p><p className="muted">{t(source.source_kind)} · {date(source.asserted_at)}</p>
      {source.source_kind === "user_settings" ? <p>{t("Profile field")}: <code>{source.field}</code> · {t("Revision")}: {source.profile_revision}</p> : <p>{t("Session")}: <code>{source.conversation_id || "—"}</code> · {t("Message ID")}: <code>{source.message_id || "—"}</code></p>}
    </blockquote>) : <p className="muted">{t("No source evidence recorded.")}</p>}
  </details>;
}

function MemoryOperation({ initial, onSettled }: { initial: Operation; onSettled: () => void }) {
  const { t } = useI18n();
  const query = useQuery<Operation>(`/operations/${segment(initial.id)}`, 2000, pollOperation);
  const operation = query.data ?? initial;
  const settled = useRef(false);
  useEffect(() => {
    if (!active(operation.state) && !settled.current) { settled.current = true; onSettled(); }
  }, [operation.state, onSettled]);
  return <div role="status" className="memory-task">
    <p>{t("Memory Operation")}: <code>{operation.id}</code> · {t(operation.state)}</p>
    {operation.state === "succeeded" && <p>{t("Memory change completed. The list has been refreshed.")}</p>}
    {(operation.state === "failed" || operation.state === "cancelled") && <p className="warning">{t("Memory change did not complete. Check Background tasks before submitting again.")}</p>}
    {operation.error && <p className="warning">{operation.error}</p>}
    <Feedback error={query.error} retry={query.refresh} />
  </div>;
}
