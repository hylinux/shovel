"use client";

import { useI18n } from "./locale-context";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Image from "next/image";
import shovelLogo from "./icon.svg";
import { segment, useAction, useAgentAction, useQuery, type Items, type WorkingMemory, type MessagePage, type Operation, type Status } from "./api";
import { Empty, Feedback, Sources } from "./ui";
import { chatTrace } from "./agent-client";
import { memoryExtractionNotice, selectedWorkingTask } from "./memory-model";
import { WorkingMemoryPanel } from "./working-memory";

export function Knowledge({ status, sessionId, onSessionChanged }: { status?: Status; sessionId: string; onSessionChanged: () => void }) {
  const { t } = useI18n();
  return <section className="session-content" aria-label={t("Chat session")}>{sessionId
    ? <Chat key={sessionId} sessionId={sessionId} status={status} onActivity={onSessionChanged} />
    : <Empty title={t("A session grounded in your documents")}>{t("Choose New Session or open a session from the sidebar.")}</Empty>}
  </section>;
}

function Chat({ sessionId, status, onActivity }: { sessionId: string; status?: Status; onActivity: () => void }) {
  const { t } = useI18n();
  const [before, setBefore] = useState<number>();
  const history = useQuery<MessagePage>(`/conversations/${segment(sessionId)}/messages?limit=100${before ? `&before=${before}` : ""}`);
  const [content, setContent] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const working = useQuery<Items<WorkingMemory>>(`/conversations/${segment(sessionId)}/working-memory`, 5000);
  const [workingTaskId, setWorkingTaskId] = useState("");
  const chosenTask = working.error || working.loading ? null : selectedWorkingTask(working.data?.items ?? [], workingTaskId);
  const workingUnavailable = Boolean(workingTaskId && !chosenTask);
  const action = useAgentAction();
  const messagesRef = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !action.busy) questionRef.current?.focus({ preventScroll: true });
    wasBusy.current = action.busy;
  }, [action.busy]);
  useEffect(() => {
    if (before && !action.busy) return;
    const messages = messagesRef.current;
    if (messages) messages.scrollTop = messages.scrollHeight;
  }, [before, history.data, history.loading, pendingQuestion, action.busy, action.text, action.tool]);
  const sendDisabled = action.busy || !content.trim() || !status?.model_configured || !status?.memory_configured || history.loading || Boolean(history.error) || workingUnavailable;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendDisabled) return;
    const question = content.trim();
    const runId = crypto.randomUUID();
    chatTrace("chat.submit", { run_id: runId, conversation_id: sessionId, input_chars: question.length });
    setPendingQuestion(question);
    const response = await action.run(sessionId, { content: question, resource_ids: [], memory_enabled: true, search_mode: "auto", working_task_id: chosenTask }, runId);
    chatTrace("chat.settled", { run_id: runId, conversation_id: sessionId, success: response });
    if (response) { setContent(""); setBefore(undefined); }
    onActivity();
    setPendingQuestion("");
    // Failed model calls may still have persisted user and failed assistant messages.
    history.refresh();
    working.refresh();
    chatTrace("chat.history.refresh.requested", { run_id: runId, conversation_id: sessionId });
  }
  return <><div ref={messagesRef} className="messages" aria-label={t("Session messages")} aria-busy={history.loading || action.busy}>
    {(history.data?.next_before || before) && <div className="button-row">
      {history.data?.next_before && <button disabled={action.busy} onClick={() => setBefore(history.data?.next_before ?? undefined)}>{t("Older messages")}</button>}
      {before && <button disabled={action.busy} onClick={() => setBefore(undefined)}>{t("Latest messages")}</button>}
    </div>}
    <Feedback error={history.error} retry={history.refresh} />
    {history.loading ? <p role="status">{t("Loading session…")}</p> : history.data?.items.length === 0 ? <Empty title={t("What would you like to find out?")}>{t("Ask a specific question about your indexed documents. Answers include source excerpts when available.")}</Empty> : history.data?.items.map((message) =>
      <article className={`message ${message.role}`} key={message.id}><MessageHeading user={message.role === "user"} failed={message.state === "failed"} />
        <p className="preserve">{message.content || (message.state === "failed" ? t("The model could not complete this response.") : "")}</p>
        {message.warnings?.map((warning, index) => <p className="warning" key={index}>{warning}</p>)}<Sources items={message.citations || []} collapsible />
        {message.memory_operation_id && <MemoryTaskStatus id={message.memory_operation_id} />}</article>)}
    {action.busy && <div role="status" className="pending-answer">
      <article className="message user"><MessageHeading user /><p className="preserve">{pendingQuestion}</p></article>
      <article className="message assistant"><MessageHeading user={false} />{action.text ? <p className="preserve">{action.text}</p> : <p className="muted">{t("Shovel is preparing an answer…")}</p>}{action.tool && <p className="muted">{t("Tool: ")}{action.tool}</p>}</article>
    </div>}
  </div><WorkingMemoryPanel sessionId={sessionId} query={working} selected={workingTaskId} onSelect={setWorkingTaskId} disabled={action.busy} /><form className="composer" onSubmit={submit}>
    {status && (!status.model_configured || !status.memory_configured) && <p className="warning">{t("Configure Chat and Memory in Settings before sending a message.")}</p>}
    <Feedback error={action.error} />
    {workingUnavailable && <p className="warning" role="alert">{t("The selected working task is unavailable. Refresh or select an active task, or explicitly choose No working task before sending.")}</p>}
    <label htmlFor="question">{t("Message")}</label>
    <textarea ref={questionRef} id="question" value={content} onChange={(event) => setContent(event.target.value)} onKeyDown={(event) => {
      if (event.key === "Enter" && event.ctrlKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        if (!event.repeat && !sendDisabled) event.currentTarget.form?.requestSubmit();
      }
    }} placeholder={t("Ask about your documents…")} rows={3} maxLength={16000} required disabled={action.busy} aria-describedby="message-shortcuts" />
    <div className="composer-footer"><span className="muted" id="message-shortcuts">{t("Ctrl + Enter to send · Enter for a new line")}</span>
      <button className="primary" aria-keyshortcuts="Control+Enter" disabled={sendDisabled}>{action.busy ? t("Waiting for model…") : t("Send Message")}</button></div>
  </form></>;
}

function MessageHeading({ user, failed = false }: { user: boolean; failed?: boolean }) {
  const { t } = useI18n();
  return <h3 className="message-heading">
    <span className="message-avatar" aria-hidden="true">{user
      ? <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="3.5" /><path d="M5 21v-2a7 7 0 0 1 14 0v2" /></svg>
      : <Image src={shovelLogo} width={32} height={32} alt="" unoptimized />}</span>
    <span>{user ? t("You") : t("Shovel")}</span>
    {failed && <span className="badge bad">{t("Failed")}</span>}
  </h3>;
}

function memoryTaskPending(operation: Operation) {
  return operation.state === "queued" || operation.state === "running";
}

function MemoryTaskStatus({ id }: { id: string }) {
  const { t } = useI18n();
  const task = useQuery<Operation>(`/operations/${segment(id)}`, 2000, memoryTaskPending);
  const action = useAction();
  const operation = task.data;
  async function retry() {
    if (await action.run<Operation>(`/operations/${segment(id)}/retry`, {})) task.refresh();
  }
  return <div className="memory-task">
    <p className={operation?.state === "failed" ? "warning" : "muted"} role="status">{t("Long-term Memory: ")}{operation
      ? memoryTaskPending(operation) ? t("updating in background") : operation.state === "succeeded" ? t(memoryExtractionNotice(operation.result)) : t(operation.state)
      : t("checking…")}</p>
    {operation?.error && <p className="muted">{t(operation.error)}</p>}
    {operation?.state === "failed" && <button disabled={action.busy} onClick={retry}>{action.busy ? t("Retrying…") : t("Retry Memory update")}</button>}
    <Feedback error={task.error || action.error} retry={task.error ? task.refresh : undefined} />
  </div>;
}
