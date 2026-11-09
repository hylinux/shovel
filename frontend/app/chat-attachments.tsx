"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { api, ApiError, errorText, segment } from "./api";
import { attachmentPath } from "./message-format";
import { useI18n } from "./locale-context";
import type { SessionDraft } from "./session-drafts";

export type Attachment = {
  id: string; name: string; media_type: string; size: number; url: string; expires_at: number;
  model_context: "text" | "image";
};
export type Upload = { id: string; file: File; attachment?: Attachment; uploading: boolean; error?: string };
export const attachmentAccept = ".txt,.md,.csv,.tsv,.json,.xml,.html,.htm,.css,.log,.c,.h,.cpp,.rs,.go,.java,.sql,.py,.js,.ts,.tsx,.jsx,.yaml,.yml,.toml,.ini,.cfg,.sh,.png,.jpg,.jpeg,.gif,.webp,.docx,.xlsx,.pptx";
export const maxAttachmentBytes = 10 * 1024 * 1024;
export const maxAttachments = 5;

export function useAttachments(sessionId: string, draft: SessionDraft) {
  const { uploads, error, notice } = useSyncExternalStore(draft.subscribe, draft.getSnapshot, draft.getSnapshot);
  const requests = draft.requests;
  function setUploads(update: (current: Upload[]) => Upload[]) { draft.update(current => ({ uploads: update(current.uploads) })); }
  function setError(error: string) { draft.update({ error }); }
  function setNotice(notice: string) { draft.update({ notice }); }
  useEffect(() => {
    const expiry = uploads.reduce((earliest, item) => item.attachment && !item.error
      ? Math.min(earliest, item.attachment.expires_at * 1000) : earliest, Infinity);
    if (!Number.isFinite(expiry)) return;
    function expire() {
      draft.update(current => ({ uploads: current.uploads.map(item => item.attachment && item.attachment.expires_at * 1000 <= Date.now()
        ? { ...item, error: "Attachment expired. Remove it and attach the file again." } : item) }));
    }
    const timer = setTimeout(expire, Math.max(0, Math.min(expiry - Date.now(), 2_147_483_647)));
    return () => clearTimeout(timer);
  }, [draft, uploads]);
  async function upload(id: string, file: File) {
    if (requests.has(id)) return;
    const request = new AbortController();
    requests.set(id, request);
    setUploads(current => current.map(item => item.id === id ? { ...item, uploading: true, error: undefined } : item));
    try {
      const attachment = await api<Attachment>(`/conversations/${segment(sessionId)}/attachments/${segment(id)}?filename=${encodeURIComponent(file.name)}`, {
        method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file, signal: request.signal,
      });
      if (!request.signal.aborted) setUploads(current => current.map(item => item.id === id ? { ...item, attachment, uploading: false } : item));
    } catch (failure) {
      if (!request.signal.aborted) setUploads(current => current.map(item => item.id === id ? { ...item, error: errorText(failure), uploading: false } : item));
    } finally { requests.delete(id); }
  }
  function add(files: File[]) {
    setError(""); setNotice("");
    for (const file of files) {
      if (draft.getSnapshot().uploads.length >= maxAttachments) { setError("Attach at most 5 files per message."); break; }
      if (!file.size || file.size > maxAttachmentBytes) { setError("Files must be nonempty and at most 10 MiB each."); continue; }
      const id = crypto.randomUUID();
      setUploads(current => [...current, { id, file, uploading: true }]);
      void upload(id, file);
    }
  }
  async function remove(id: string) {
    setError(""); setNotice("");
    requests.get(id)?.abort();
    try {
      await api(`/conversations/${segment(sessionId)}/attachments/${segment(id)}`, { method: "DELETE" });
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 && failure.code === "AttachmentBound") {
        setNotice("Removed from draft. The file remains available in Session history until expiry.");
      } else if (!(failure instanceof ApiError && failure.status === 404)) { setError(errorText(failure)); return; }
    }
    setUploads(current => current.filter(item => item.id !== id));
  }
  function clear() { setUploads(() => []); setError(""); setNotice(""); }
  return { uploads, error, notice, add, remove, retry: upload, clear, uploading: uploads.some(item => item.uploading),
    completed: uploads.flatMap(item => item.attachment && item.attachment.expires_at * 1000 > Date.now() ? [item.attachment] : []),
    invalid: uploads.some(item => !item.attachment || Boolean(item.error) || item.attachment.expires_at * 1000 <= Date.now()) };
}

function UploadedImage({ src, name, thumbnail = false }: { src: string; name: string; thumbnail?: boolean }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  if (failed) return <small className="warning">{t("Image preview unavailable or expired.")}</small>;
  // Only preview backend-validated uploads, not unvalidated blob/data URLs.
  // eslint-disable-next-line @next/next/no-img-element
  return <img className={thumbnail ? "upload-thumbnail" : "message-image"} src={src} alt={name} loading="lazy" onError={() => setFailed(true)} />;
}

export function UploadList({ uploads, remove, retry, disabled, sessionId }: { uploads: Upload[]; remove: (id: string) => Promise<void>; retry: (id: string, file: File) => Promise<void>; disabled: boolean; sessionId: string }) {
  const { t } = useI18n();
  return <ul className="upload-list">{uploads.map(item => <li key={item.id}>
    {item.attachment?.media_type.startsWith("image/") && <UploadedImage src={attachmentPath(sessionId, item.id)} name={item.file.name} thumbnail />}
    <span className="upload-name">{item.file.name}<small>{Math.ceil(item.file.size / 1024)} {"KB"} · {item.uploading ? t("Uploading…") : item.error ? t("Upload failed") : t("Ready")}</small></span>
    {item.error && <span className="warning" role="alert">{t(item.error)}</span>}
    {item.error && <button type="button" disabled={disabled || item.uploading} onClick={() => void retry(item.id, item.file)}>{t("Retry")}</button>}
    <button type="button" disabled={disabled || item.uploading} onClick={() => void remove(item.id)} aria-label={t("Remove attachment {p0}", { p0: item.file.name })}>{"×"}</button>
  </li>)}</ul>;
}

export function MessageAttachments({ items, sessionId }: { items: Attachment[]; sessionId: string }) {
  const { t, date } = useI18n();
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => clearInterval(timer);
  }, []);
  if (!items.length) return null;
  return <div className="message-attachments">{items.map(item => {
    const url = attachmentPath(sessionId, item.id);
    const expired = item.expires_at <= now;
    return <figure className="attachment-card" key={item.id}>
      {!expired && item.media_type.startsWith("image/") && <a className="message-image-link" href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
        <UploadedImage src={url} name={item.name} />
      </a>}
      <figcaption>{expired ? <span>{item.name} · {t("Attachment expired")}</span> : <a href={`${url}?download=true`} download={item.name}>{item.name}</a>}
        <small>{Math.ceil(item.size / 1024)} {"KB"} · {t("Expires")} {date(item.expires_at)}</small>
        <small>{t(item.model_context === "image" ? "Image input for the model" : "Text excerpt for the model")}</small>
      </figcaption>
    </figure>;
  })}</div>;
}
