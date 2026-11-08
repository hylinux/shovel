"use client";

import { useState, type FormEvent } from "react";
import { api, errorText, segment, type Items, type WorkingMemory } from "./api";
import { lines, newWorkingTask, workingPayload, writeWorkingTask } from "./memory-model";
import { useI18n } from "./locale-context";
import { Feedback } from "./ui";

type WorkingQuery = { data?: Items<WorkingMemory>; loading: boolean; error: string; refresh: () => void };

export function WorkingMemoryPanel({ sessionId, query, selected, onSelect, disabled }: {
  sessionId: string; query: WorkingQuery; selected: string; onSelect: (id: string) => void; disabled: boolean;
}) {
  const { t, date } = useI18n();
  const [draft, setDraft] = useState<WorkingMemory>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [mustReload, setMustReload] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const base = `/conversations/${segment(sessionId)}/working-memory`;
  const items = query.data?.items ?? [];
  const activeTasks = items.filter(item => item.state === "active");
  const selectedUnavailable = Boolean(selected && (query.error || query.loading || !activeTasks.some(item => item.task_id === selected)));
  const expired = draft?.state === "expired" || items.some(item => item.task_id === draft?.task_id && item.state === "expired");
  function edit(task: WorkingMemory) {
    setDraft(task); setMustReload(false); setConfirmDelete(false); setError(""); setNotice("");
  }
  async function reload() {
    if (!draft || busy) return;
    setBusy(true);
    try {
      const latest = await api<Items<WorkingMemory>>(base);
      const task = latest.items.find(item => item.task_id === draft.task_id);
      if (task) edit(task);
      else { setDraft(undefined); setMustReload(false); setError(""); setNotice("This working task is no longer saved."); }
      query.refresh();
    } catch (error) { setError(errorText(error)); }
    finally { setBusy(false); }
  }
  async function save(event?: FormEvent, remove = false) {
    event?.preventDefault();
    if (!draft || busy || mustReload) return;
    if (expired && !remove) return;
    try { if (!remove) workingPayload(draft); }
    catch (error) { setError(errorText(error)); return; }
    setBusy(true); setError(""); setNotice("");
    const outcome = await writeWorkingTask(api, base, draft, remove);
    setBusy(false); query.refresh();
    if (!outcome.ok) {
      setMustReload(outcome.needsReload);
      setError(errorText(outcome.error) + (outcome.needsReload && !outcome.rechecked
        ? ` ${t("Snapshot reload also failed: {p0}", { p0: errorText(outcome.reconciliationError) })}` : ""));
      if (outcome.needsReload) setNotice(outcome.rechecked
        ? "The saved snapshot was rechecked. Reload it before writing again; your draft is kept until then."
        : "The saved snapshot could not be rechecked. Reload it before writing again; your draft is kept until then.");
      return;
    }
    if (remove) { setDraft(undefined); setConfirmDelete(false); if (selected === draft.task_id) onSelect(""); }
    else if ("task_id" in outcome.result) {
      setDraft(outcome.result);
      if (outcome.result.state === "active") onSelect(outcome.result.task_id);
    }
    setNotice(remove ? "Working task deleted." : "Working notes saved. This does not complete a business action.");
  }
  return <details className="working-memory settings-details">
    <summary>{t("Working context for this Session")}</summary>
    <p className="muted">{t("User-maintained task notes, not model-generated facts or execution progress. Only the selected active task is included when Memory is enabled. Execution remains authoritative for business actions.")}</p>
    <Feedback error={query.error} retry={query.refresh} />
    <div className="button-row">
      <label>{t("Context task")}<select value={selected} disabled={disabled || busy} onChange={event => onSelect(event.target.value)}>
        <option value="">{t("No working task")}</option>
        {selectedUnavailable && <option value={selected} disabled>{t("Unavailable working task: {p0}", { p0: selected })}</option>}
        {activeTasks.filter(item => !selectedUnavailable || item.task_id !== selected).map(item => <option key={item.task_id} value={item.task_id} disabled={query.loading || Boolean(query.error)}>{item.goal || item.task_id}</option>)}
      </select></label>
      <button type="button" disabled={disabled || busy || mustReload} onClick={() => edit(newWorkingTask(crypto.randomUUID()))}>{t("New working task")}</button>
      <button type="button" disabled={busy} onClick={query.refresh}>{t("Refresh")}</button>
    </div>
    {query.loading && <p role="status">{t("Loading working tasks…")}</p>}
    {items.map(item => <div className="working-task-row" key={item.task_id}>
      <span className="preserve">{item.goal}</span><span className="badge">{t(item.state)}</span>
      <span className="muted">{t("Revision")} {item.revision} · {date(item.updated_at)}</span>
      <button type="button" disabled={disabled || busy || mustReload} onClick={() => edit(item)}>{t(item.state === "expired" ? "Inspect notes" : "Edit")}</button>
    </div>)}
    <Feedback error={error} notice={notice} />
    {draft && <form className="memory-editor" onSubmit={event => save(event)}>
      <p className="muted">{t("Task ID")}: <code>{draft.task_id}</code> · {t("Revision")} {draft.revision}</p>
      <fieldset disabled={busy || disabled || mustReload}>
        <label>{t("Goal")}<textarea readOnly={expired} value={draft.goal} rows={2} maxLength={1000} required onChange={event => setDraft({ ...draft, goal: event.target.value })} /></label>
        {(["constraints", "decisions", "plan", "blockers", "evidence_message_ids"] as const).map(field => <label key={field}>{t({ constraints: "Constraints", decisions: "Decisions", plan: "Plan", blockers: "Blockers", evidence_message_ids: "Evidence message IDs" }[field])}
          <textarea readOnly={expired} rows={2} value={draft[field].join("\n")} onChange={event => setDraft({ ...draft, [field]: lines(event.target.value) })} />
        </label>)}
        <p className="muted">{t("One entry per line; up to 20 entries, 500 characters each. Evidence IDs must refer to messages in this Session.")}</p>
        <label>{t("Next step")}<textarea readOnly={expired} rows={2} maxLength={1000} value={draft.next_step} onChange={event => setDraft({ ...draft, next_step: event.target.value })} /></label>
        <label>{t("State")}<select disabled={expired} value={expired ? "expired" : draft.state} onChange={event => setDraft({ ...draft, state: event.target.value as WorkingMemory["state"] })}>
          {["active", "completed", "archived"].map(state => <option key={state} value={state}>{t(state)}</option>)}
          {expired && <option value="expired">{t("expired")}</option>}
        </select></label>
        <label>{t("Expires at (UTC Unix seconds)")}<input readOnly={expired} type="number" min={0} step={1} value={draft.expires_at ?? ""} onChange={event => setDraft({ ...draft, expires_at: event.target.value ? Number(event.target.value) : null })} /></label>
        <p className="muted">{t("Blank expiry uses configured task retention. Expired tasks cannot be edited or selected.")}</p>
        {draft.last_user_message_id && <p>{t("Last user message")}: <code>{draft.last_user_message_id}</code></p>}
        {draft.last_assistant_message_id && <p>{t("Last assistant message")}: <code>{draft.last_assistant_message_id}</code></p>}
        <div className="button-row"><button className="primary" type="submit" disabled={expired}>{t("Save working notes")}</button>
          {draft.revision > 0 && <button className="danger" type="button" onClick={() => setConfirmDelete(true)}>{t("Delete working task…")}</button>}
          <button type="button" onClick={() => setDraft(undefined)}>{t("Cancel")}</button></div>
        {confirmDelete && <div className="confirmation"><p>{t("Delete these contextual notes? Session messages and executions are kept.")}</p><button className="danger" type="button" onClick={() => save(undefined, true)}>{t("Confirm delete")}</button><button type="button" onClick={() => setConfirmDelete(false)}>{t("Cancel")}</button></div>}
      </fieldset>
      {mustReload && <button type="button" disabled={busy} onClick={reload}>{t("Reload saved snapshot")}</button>}
    </form>}
  </details>;
}
