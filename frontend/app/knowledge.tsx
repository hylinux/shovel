"use client";

import { useI18n } from "./locale-context";

import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import Image from "next/image";
import shovelLogo from "./icon.svg";
import { segment, useAction, useAgentAction, useQuery, type Items, type WorkingMemory, type MessagePage, type Operation, type Status } from "./api";
import { Empty, Feedback, Sources } from "./ui";
import { chatTrace } from "./agent-client";
import { memoryExtractionNotice, selectedWorkingTask } from "./memory-model";
import { WorkingMemoryPanel } from "./working-memory";
import { MessageContent, RichText } from "./message-content";
import { ComposerTools } from "./composer-tools";
import { formattedMessage, type MessageFormat } from "./message-format";
import { attachmentAccept, MessageAttachments, UploadList, useAttachments, type Attachment } from "./chat-attachments";
import type { SessionDraft, SessionDrafts } from "./session-drafts";
import { UserAvatar, useUserProfile } from "./user-profile";

export function Knowledge({ status, sessionId, onSessionChanged, drafts }: { status?: Status; sessionId: string; onSessionChanged: () => void; drafts: SessionDrafts }) {
  const { t } = useI18n();
  const profile = useUserProfile();
  return <section className="session-content" aria-label={t("Chat session")}><Feedback error={profile.error} retry={profile.refresh} />{sessionId
    ? status?.workspace_id ? <Chat key={JSON.stringify([status.workspace_id, sessionId])} sessionId={sessionId} status={status} onActivity={onSessionChanged} draft={drafts.get(status.workspace_id, sessionId)} />
      : <p role="status">{t("Loading session…")}</p>
    : <Empty title={t("A session grounded in your documents")}>{t("Choose New Session or open a session from the sidebar.")}</Empty>}
  </section>;
}

function Chat({ sessionId, status, onActivity, draft }: { sessionId: string; status?: Status; onActivity: () => void; draft: SessionDraft }) {
  const { t } = useI18n();
  const [before, setBefore] = useState<number>();
  const history = useQuery<MessagePage>(`/conversations/${segment(sessionId)}/messages?limit=100${before ? `&before=${before}` : ""}`);
  const { content, format, preview } = useSyncExternalStore(draft.subscribe, draft.getSnapshot, draft.getSnapshot);
  const setContent = (content: string) => draft.update({ content });
  const setFormat = (format: MessageFormat) => draft.update({ format });
  const setPreview = (preview: boolean) => draft.update({ preview });
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  const attachments = useAttachments(sessionId, draft);
  const working = useQuery<Items<WorkingMemory>>(`/conversations/${segment(sessionId)}/working-memory`, 5000);
  const [workingTaskId, setWorkingTaskId] = useState("");
  const chosenTask = working.error || working.loading ? null : selectedWorkingTask(working.data?.items ?? [], workingTaskId);
  const workingUnavailable = Boolean(workingTaskId && !chosenTask);
  const action = useAgentAction();
  const messagesRef = useRef<HTMLDivElement>(null);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
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
  const outgoing = content.trim() ? formattedMessage(content, format) : "";
  const tooLong = outgoing.length > 16000;
  const sendDisabled = action.busy || attachments.uploading || attachments.invalid || tooLong || (!content.trim() && !attachments.completed.length) || !status?.model_configured || !status?.memory_configured || history.loading || Boolean(history.error) || workingUnavailable;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendDisabled) return;
    const sentDraft = draft.getSnapshot();
    const question = outgoing;
    const runId = crypto.randomUUID();
    chatTrace("chat.submit", { run_id: runId, conversation_id: sessionId, input_chars: question.length });
    setPendingQuestion(question);
    setPendingAttachments(attachments.completed);
    const response = await action.run(sessionId, { content: question, resource_ids: [], memory_enabled: true, search_mode: "auto", working_task_id: chosenTask, attachment_ids: attachments.completed.map(item => item.id) }, runId);
    chatTrace("chat.settled", { run_id: runId, conversation_id: sessionId, success: response });
    if (response) { draft.clearAfterSend(sentDraft); setBefore(undefined); }
    onActivity();
    setPendingQuestion("");
    setPendingAttachments([]);
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
        <MessageContent content={message.content || (message.state === "failed" ? t("The model could not complete this response.") : "")} sessionId={sessionId} />
        <MessageAttachments items={message.attachments || []} sessionId={sessionId} />
        {message.warnings?.map((warning, index) => <p className="warning" key={index}>{warning}</p>)}<Sources items={message.citations || []} collapsible />
        {message.memory_operation_id && <MemoryTaskStatus id={message.memory_operation_id} />}</article>)}
    {action.busy && <div role="status" className="pending-answer">
      <article className="message user"><MessageHeading user /><MessageContent content={pendingQuestion} sessionId={sessionId} /><MessageAttachments items={pendingAttachments} sessionId={sessionId} /></article>
      <article className="message assistant"><MessageHeading user={false} />{action.text ? <MessageContent content={action.text} sessionId={sessionId} streaming /> : <p className="muted">{t("Shovel is preparing an answer…")}</p>}{action.tool && <p className="muted">{t("Tool: ")}{action.tool}</p>}</article>
    </div>}
  </div><WorkingMemoryPanel sessionId={sessionId} query={working} selected={workingTaskId} onSelect={setWorkingTaskId} disabled={action.busy} /><form className="composer" onSubmit={submit}>
    {status && (!status.model_configured || !status.memory_configured) && <p className="warning">{t("Configure Chat and Memory in Settings before sending a message.")}</p>}
    <Feedback error={action.error} />
    {workingUnavailable && <p className="warning" role="alert">{t("The selected working task is unavailable. Refresh or select an active task, or explicitly choose No working task before sending.")}</p>}
    <label htmlFor="question">{t("Message")}</label>
    <div className="composer-editor"><ComposerTools value={content} onChange={setContent} textarea={questionRef} disabled={action.busy} format={format} onFormat={setFormat} preview={preview} onPreview={() => setPreview(!preview)} />
    {preview && <div className="composer-preview"><RichText content={formattedMessage(content, format)} sessionId={sessionId} /></div>}
    <textarea ref={questionRef} id="question" hidden={preview} value={content} onChange={(event) => setContent(event.target.value)} onPaste={event => {
      const files = Array.from(event.clipboardData.files);
      if (files.length && !action.busy) { event.preventDefault(); attachments.add(files); }
    }} onKeyDown={(event) => {
      if (event.key === "Enter" && event.ctrlKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        if (!event.repeat && !sendDisabled) event.currentTarget.form?.requestSubmit();
      }
    }} placeholder={t("Write Markdown, paste an image, or attach a file…")} rows={4} maxLength={16000} required={!attachments.completed.length} disabled={action.busy} aria-describedby="message-shortcuts attachment-help" />
    <UploadList uploads={attachments.uploads} remove={attachments.remove} retry={attachments.retry} disabled={action.busy} sessionId={sessionId} /></div>
    {attachments.error && <p className="warning" role="alert">{t(attachments.error)}</p>}
    {attachments.notice && <p className="muted" role="status">{t(attachments.notice)}</p>}
    {tooLong && <p className="warning" role="alert">{t("The formatted message exceeds 16,000 characters. Shorten it or attach a text file.")}</p>}
    <div className="composer-footer"><div className="composer-attach"><input ref={fileRef} type="file" multiple accept={attachmentAccept} className="sr-only" tabIndex={-1} aria-label={t("Attach files")} disabled={action.busy} onChange={event => { attachments.add(Array.from(event.target.files || [])); event.target.value = ""; }} />
      <button type="button" disabled={action.busy || attachments.uploading} onClick={() => fileRef.current?.click()}>{t("Attach files")}</button><span className="muted" id="message-shortcuts">{t("Ctrl + Enter to send · Enter for a new line")}</span></div>
      <button className="primary" aria-keyshortcuts="Control+Enter" disabled={sendDisabled}>{action.busy ? t("Waiting for model…") : t("Send Message")}</button></div>
    <small className="muted attachment-help" id="attachment-help">{t("Up to 5 files, 10 MiB each. Attachments are temporary and sent to the configured model. Only attach content you are allowed to share.")}</small>
    <details className="attachment-policy"><summary>{t("Attachment limits and privacy")}</summary>
      <p>{t("Uploads stay local until you send. Pending files expire after 24 hours; sent files expire after 7 days. Deleting the Session removes its files.")}</p>
      <p>{t("Images are sanitized, resized to at most 2048 × 2048 and converted to JPEG; animation and metadata are removed. Text and Office files provide excerpts of up to 8,000 characters per file. PDF, SVG, executables and archives are not accepted.")}</p>
      <p>{t("Image understanding requires a vision-capable model. Files are untrusted reference data, not executable instructions or indexed resources.")}</p>
    </details>
  </form></>;
}

export function MessageHeading({ user, failed = false }: { user: boolean; failed?: boolean }) {
  const { t } = useI18n();
  const { profile } = useUserProfile();
  return <h3 className="message-heading">
    <span className="message-avatar" aria-hidden="true">{user
      ? <UserAvatar src={profile?.avatar_url} />
      : <Image src={shovelLogo} width={32} height={32} alt="" unoptimized />}</span>
    <span>{user ? profile?.nickname || t("You") : t("Shovel")}</span>
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
