"use client";

import { useI18n } from "./locale-context";

import { memo, useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import Image from "next/image";
import shovelLogo from "./icon.svg";
import { segment, useAction, useAgentAction, useQuery, type Items, type WorkingMemory, type Message, type MessagePage, type Operation, type Status } from "./api";
import { Empty, Feedback, Sources } from "./ui";
import { chatTrace } from "./agent-client";
import { memoryExtractionNotice, selectedWorkingTask } from "./memory-model";
import { WorkingMemoryPanel } from "./working-memory";
import { MessageContent, RichText } from "./message-content";
import { ComposerTools } from "./composer-tools";
import { formattedMessage, pastedText, type MessageFormat } from "./message-format";
import { MessageAttachments, UploadList, useAttachments, type Attachment, type AttachmentStatus } from "./chat-attachments";
import { useSessionScroll } from "./session-scroll";
import type { SessionDraft, SessionDrafts } from "./session-drafts";
import { UserAvatar, useUserProfile } from "./user-profile";
import { SendBudgetNotice, useSendBudget } from "./send-budget";

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
  const historyPath = `/conversations/${segment(sessionId)}/messages?limit=100${before ? `&before=${before}` : ""}`;
  const history = useQuery<MessagePage>(historyPath);
  const { content, format, preview } = useSyncExternalStore(draft.subscribe, draft.getSnapshot, draft.getSnapshot);
  const setContent = (content: string) => draft.update({ content });
  const setFormat = (format: MessageFormat) => draft.update({ format });
  const setPreview = (preview: boolean) => draft.update({ preview });
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  const [pasteNotice, setPasteNotice] = useState("");
  const attachmentStatus = useQuery<AttachmentStatus>(`/conversations/${segment(sessionId)}/attachments`, 30000);
  const attachments = useAttachments(sessionId, draft, attachmentStatus.error ? undefined : attachmentStatus.data, attachmentStatus.refresh);
  const working = useQuery<Items<WorkingMemory>>(`/conversations/${segment(sessionId)}/working-memory`, 5000);
  const [workingTaskId, setWorkingTaskId] = useState("");
  const chosenTask = working.error || working.loading ? null : selectedWorkingTask(working.data?.items ?? [], workingTaskId);
  const workingUnavailable = Boolean(workingTaskId && !chosenTask);
  const action = useAgentAction();
  const messagesRef = useRef<HTMLDivElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const historyReady = !history.loading && (!history.dataPath || history.dataPath === historyPath);
  const scroll = useSessionScroll(messagesRef, messageListRef, before ? String(before) : "latest", historyReady,
    `${history.data?.items.at(-1)?.id ?? ""}:${pendingQuestion}:${action.busy}:${action.text}:${action.tool}`);
  function navigate(next?: number) { scroll.navigate(next ? String(next) : "latest"); setBefore(next); }
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  function togglePreview() {
    const element = questionRef.current;
    if (!preview && element) draft.update({ selection: {
      start: element.selectionStart, end: element.selectionEnd, direction: element.selectionDirection,
    }, preview: true });
    else setPreview(!preview);
    requestAnimationFrame(() => {
      if (!preview) previewRef.current?.focus({ preventScroll: true });
      else {
        questionRef.current?.focus({ preventScroll: true });
        const selection = draft.getSnapshot().selection;
        if (selection) questionRef.current?.setSelectionRange(selection.start, selection.end, selection.direction);
      }
    });
  }
  const fileRef = useRef<HTMLInputElement>(null);
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !action.busy) questionRef.current?.focus({ preventScroll: true });
    wasBusy.current = action.busy;
  }, [action.busy]);
  const outgoing = content.trim() ? formattedMessage(content, format) : "";
  const tooLong = outgoing.length > 16000;
  const budgetInput = !tooLong && !attachments.uploading && !attachments.invalid && !workingUnavailable && (outgoing || attachments.completed.length)
    ? { content: outgoing, resource_ids: [], memory_enabled: true, search_mode: "auto", working_task_id: chosenTask,
      attachment_ids: attachments.completed.map(item => item.id) } : null;
  const budget = useSendBudget(sessionId, budgetInput, history.data?.items.at(-1)?.id ?? "");
  const sendDisabled = action.busy || budget.blocked || attachments.uploading || attachments.invalid || tooLong || (!content.trim() && !attachments.completed.length) || !status?.model_configured || !status?.memory_configured || history.loading || Boolean(history.error) || workingUnavailable;
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
    if (response) { draft.clearAfterSend(sentDraft); if (before) navigate(); }
    onActivity();
    setPendingQuestion("");
    setPendingAttachments([]);
    // Failed model calls may still have persisted user and failed assistant messages.
    attachmentStatus.refresh();
    history.refresh();
    working.refresh();
    chatTrace("chat.history.refresh.requested", { run_id: runId, conversation_id: sessionId });
  }
  return <><div ref={messagesRef} className="messages" aria-label={t("Session messages")} aria-busy={!historyReady || action.busy}>
    <div ref={messageListRef} className="message-list">
    {(history.data?.next_before || before) && <div className="button-row">
      {history.data?.next_before && <button disabled={action.busy || !historyReady} onClick={() => navigate(history.data?.next_before ?? undefined)}>{t("Older messages")}</button>}
      {before && <button disabled={action.busy || !historyReady} onClick={() => navigate()}>{t("Latest messages")}</button>}
    </div>}
    <Feedback error={history.error} retry={history.refresh} />
    {!historyReady ? <p role="status">{t("Loading session…")}</p> : history.data?.items.length === 0 ? <Empty title={t("What would you like to find out?")}>{t("Ask a specific question about your indexed documents. Answers include source excerpts when available.")}</Empty> : history.data?.items.map((message) =>
      <HistoryMessage key={message.id} message={message} sessionId={sessionId} status={attachmentStatus.data} />)}
    {action.busy && <div role="status" className="pending-answer">
      <article className="message user"><MessageHeading user /><MessageContent content={pendingQuestion} sessionId={sessionId} /><MessageAttachments items={pendingAttachments} sessionId={sessionId} /></article>
      <article className="message assistant"><MessageHeading user={false} />{action.text ? <MessageContent content={action.text} sessionId={sessionId} streaming /> : <p className="muted">{t("Shovel is preparing an answer…")}</p>}{action.tool && <p className="muted">{t("Tool: ")}{action.tool}</p>}</article>
    </div>}
    </div>
  </div>{!before && scroll.paused && <div className="session-scroll-control"><button type="button" onClick={scroll.bottom}>
    {t(scroll.unread ? "New reply · Back to bottom" : "Back to bottom")}
  </button><span className="sr-only" role="status">{scroll.unread ? t("A new reply is available below.") : ""}</span></div>}
  <WorkingMemoryPanel sessionId={sessionId} query={working} selected={workingTaskId} onSelect={setWorkingTaskId} disabled={action.busy} /><form className="composer" onSubmit={submit}>
    {status && (!status.model_configured || !status.memory_configured) && <p className="warning">{t("Configure Chat and Memory in Settings before sending a message.")}</p>}
    <Feedback error={action.error} />
    {workingUnavailable && <p className="warning" role="alert">{t("The selected working task is unavailable. Refresh or select an active task, or explicitly choose No working task before sending.")}</p>}
    <label htmlFor="question">{t("Message")}</label>
    <div className="composer-editor"><ComposerTools value={content} onChange={setContent} textarea={questionRef} disabled={action.busy} format={format} onFormat={setFormat} preview={preview} onPreview={togglePreview} />
    <div ref={previewRef} id="composer-preview" className="composer-preview" hidden={!preview} tabIndex={-1} aria-label={t("Message preview")} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); togglePreview(); }
    }}>{preview && <RichText content={formattedMessage(content, format)} sessionId={sessionId} />}</div>
    <textarea ref={questionRef} id="question" hidden={preview} value={content} onChange={(event) => setContent(event.target.value)} onPaste={event => {
      const files = Array.from(event.clipboardData.files);
      if (files.length && !action.busy) {
        event.preventDefault();
        const element = event.currentTarget;
        const paste = pastedText(content, element.selectionStart, element.selectionEnd, event.clipboardData.getData("text/plain"));
        setContent(paste.value);
        setPasteNotice(paste.truncated ? "Pasted text was shortened to the message limit." : "");
        requestAnimationFrame(() => { element.setSelectionRange(paste.cursor, paste.cursor); });
        attachments.add(files);
      }
    }} onKeyDown={(event) => {
      if (event.key === "Enter" && event.ctrlKey && !event.nativeEvent.isComposing) {
        event.preventDefault();
        if (!event.repeat && !sendDisabled) event.currentTarget.form?.requestSubmit();
      }
    }} placeholder={t("Write Markdown, paste an image, or attach a file…")} spellCheck={format === "markdown" || format === "text"} autoCorrect={format === "markdown" || format === "text" ? "on" : "off"} autoCapitalize={format === "markdown" || format === "text" ? "sentences" : "off"} rows={4} maxLength={16000} required={!attachments.completed.length} disabled={action.busy} aria-describedby="message-shortcuts attachment-help" />
    <UploadList uploads={attachments.uploads} remove={attachments.remove} cancel={attachments.cancel} retry={attachments.retry} disabled={action.busy} sessionId={sessionId} status={attachmentStatus.data} /></div>
    <Feedback error={attachmentStatus.error} retry={attachmentStatus.refresh} />
    {pasteNotice && <p className="warning" role="status">{t(pasteNotice)}</p>}
    {attachments.error && <p className="warning" role="alert">{t(attachments.error)}</p>}
    {attachments.notice && <p className="muted" role="status">{t(attachments.notice)}</p>}
    {tooLong && <p className="warning" role="alert">{t("The formatted message exceeds 16,000 characters. Shorten it or attach a text file.")}</p>}
    {(content.length > 0 || attachments.uploads.length > 0) && <small className="muted">{t("Drafts stay in page memory. Refreshing or closing can discard text and uploads; your browser may ask before leaving.")}</small>}
    {budgetInput && <SendBudgetNotice budget={budget} />}
    <div className="composer-footer"><div className="composer-attach"><input ref={fileRef} type="file" multiple accept={attachmentStatus.data?.limits.extensions.join(",")} className="sr-only" tabIndex={-1} aria-label={t("Attach files")} disabled={action.busy || !attachmentStatus.data || Boolean(attachmentStatus.error)} onChange={event => { attachments.add(Array.from(event.target.files || [])); event.target.value = ""; }} />
      <button type="button" disabled={action.busy || attachments.uploading || !attachmentStatus.data || Boolean(attachmentStatus.error)} onClick={() => fileRef.current?.click()}>{t("Attach files")}</button><span className="muted" id="message-shortcuts">{t("Ctrl + Enter to send · Enter for a new line")}</span></div>
      <button className="primary" aria-keyshortcuts="Control+Enter" disabled={sendDisabled}>{action.busy ? t("Waiting for model…") : t("Send Message")}</button></div>
    <small className="muted attachment-help" id="attachment-help">{attachmentStatus.data ? t("Up to {p0} files, {p1} MiB each. Only attach content you are allowed to share.", { p0: attachmentStatus.data.limits.max_files, p1: attachmentStatus.data.limits.max_file_bytes / 1048576 }) : t("Loading attachment limits…")}</small>
    {attachmentStatus.data && <div className="attachment-budget"><small>{t("Remaining: {p0} pending files, {p1} Session files; {p2} MiB Session / {p3} MiB workspace storage.", {
      p0: attachmentStatus.data.remaining.pending_files, p1: attachmentStatus.data.remaining.session_files,
      p2: (attachmentStatus.data.remaining.session_bytes / 1048576).toFixed(1), p3: (attachmentStatus.data.remaining.store_bytes / 1048576).toFixed(1) })}</small>
      <small>{t("Model media limit: {p0} images, {p1} MiB including retained history.", { p0: attachmentStatus.data.limits.max_input_images, p1: attachmentStatus.data.limits.max_input_image_bytes / 1048576 })}</small>
      <small>{t("Prepared excerpts and images may be omitted by context budgets or expiry. These details do not confirm that the model read every file.")}</small>
    </div>}
    <details className="attachment-policy"><summary>{t("Attachment limits and privacy")}</summary>
      <p>{t("Uploads stay local until you send. Pending files expire after 24 hours; sent files expire after 7 days. Deleting the Session removes its files.")}</p>
      {attachmentStatus.data && <p>{t("Images: at most {p0} pixels before decoding, resized to {p1} × {p1}. Text/Office excerpt limit: {p2} characters per file.", { p0: attachmentStatus.data.limits.max_image_pixels, p1: attachmentStatus.data.limits.max_image_edge, p2: attachmentStatus.data.limits.max_text_chars })}</p>}
      <p>{t("Image understanding requires a vision-capable model. Files are untrusted reference data, not executable instructions or indexed resources.")}</p>
    </details>
  </form></>;
}

const HistoryMessage = memo(function HistoryMessage({ message, sessionId, status }: { message: Message; sessionId: string; status?: AttachmentStatus }) {
  const { t } = useI18n();
  return <article className={`message ${message.role}`} data-message-id={message.id}><MessageHeading user={message.role === "user"} failed={message.state === "failed"} />
    <MessageContent content={message.content || (message.state === "failed" ? t("The model could not complete this response.") : "")} sessionId={sessionId} />
    <MessageAttachments items={message.attachments || []} sessionId={sessionId} status={status} />
    {message.warnings?.map((warning, index) => <p className="warning" key={index}>{warning}</p>)}<Sources items={message.citations || []} collapsible />
    {message.memory_operation_id && <MemoryTaskStatus id={message.memory_operation_id} />}</article>;
});

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
