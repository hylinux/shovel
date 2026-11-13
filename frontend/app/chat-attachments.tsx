"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { api, ApiError, errorText, segment, uploadFile } from "./api";
import { attachmentPath } from "./message-format";
import { useI18n } from "./locale-context";
import type { SessionDraft } from "./session-drafts";

export type Attachment = {
  id: string; name: string; media_type: string; size: number; url: string; expires_at: number;
  model_context: "text" | "image";
  reading_scope?: { original_bytes?: number; stored_bytes?: number; original_width?: number; original_height?: number;
    width?: number; height?: number; excerpt_chars?: number; truncated?: boolean };
  available?: boolean;
};
export type AttachmentStatus = {
  items: Attachment[];
  limits: { extensions: string[]; max_files: number; max_file_bytes: number; max_text_chars: number;
    max_image_pixels: number; max_image_edge: number; max_input_images: number; max_input_image_bytes: number };
  remaining: { pending_files: number; session_files: number; session_bytes: number; store_bytes: number };
};
export type Upload = { id: string; file: File; attachment?: Attachment; uploading: boolean; progress?: number; error?: string };

export function useAttachments(sessionId: string, draft: SessionDraft, status?: AttachmentStatus, refresh: () => void = () => {}) {
  const { uploads, error, notice } = useSyncExternalStore(draft.subscribe, draft.getSnapshot, draft.getSnapshot);
  const requests = draft.requests;
  function setUploads(update: (current: Upload[]) => Upload[]) { draft.update(current => ({ uploads: update(current.uploads) })); }
  function setError(error: string) { draft.update({ error }); }
  function setNotice(notice: string) { draft.update({ notice }); }
  const unavailable = (attachment: Attachment) => attachment.expires_at * 1000 <= Date.now()
    || attachment.available === false || status?.items.some(item => item.id === attachment.id && item.available === false);
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
    if (requests.has(id) || !draft.getSnapshot().uploads.some(item => item.id === id)) return;
    const request = new AbortController();
    requests.set(id, request);
    setUploads(current => current.map(item => item.id === id ? { ...item, uploading: true, progress: 0, error: undefined } : item));
    try {
      const attachment = await uploadFile<Attachment>(`/conversations/${segment(sessionId)}/attachments/${segment(id)}?filename=${encodeURIComponent(file.name)}`, file, request.signal, fraction => {
        if (!request.signal.aborted && requests.get(id) === request)
          setUploads(current => current.map(item => item.id === id ? { ...item, progress: fraction } : item));
      });
      if (!request.signal.aborted) setUploads(current => current.map(item => item.id === id ? { ...item, attachment, uploading: false } : item));
    } catch (failure) {
      if (!request.signal.aborted) setUploads(current => current.map(item => item.id === id ? { ...item, error: errorText(failure), uploading: false } : item));
    } finally { if (requests.get(id) === request) requests.delete(id); refresh(); }
  }
  function add(files: File[]) {
    setError(""); setNotice("");
    if (!status) { setError("Attachment limits are unavailable. Refresh before attaching files."); return; }
    for (const file of files) {
      if (draft.getSnapshot().uploads.length >= status.limits.max_files) { setError("The message attachment limit has been reached."); break; }
      if (!file.size || file.size > status.limits.max_file_bytes) { setError("The file is empty or exceeds the server upload limit."); continue; }
      const suffix = "." + file.name.split(".").at(-1)?.toLowerCase();
      if (!status.limits.extensions.includes(suffix)) { setError("Unsupported attachment type."); continue; }
      const id = crypto.randomUUID();
      setUploads(current => [...current, { id, file, uploading: true }]);
      void upload(id, file);
    }
  }
  function cancel(id: string) {
    requests.get(id)?.abort();
    requests.delete(id);
    setUploads(current => current.map(item => item.id === id ? { ...item, uploading: false,
      error: "Upload cancelled. The server may have received it; retry to recover the same upload, or remove it." } : item));
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
    refresh();
  }
  function clear() { setUploads(() => []); setError(""); setNotice(""); }
  return { uploads, error, notice, add, remove, cancel, retry: upload, clear, uploading: uploads.some(item => item.uploading),
    completed: uploads.flatMap(item => item.attachment && !item.error && !unavailable(item.attachment) ? [item.attachment] : []),
    invalid: uploads.some(item => !item.attachment || Boolean(item.error) || unavailable(item.attachment)) };
}

function UploadedImage({ src, name, thumbnail = false, attachment }: { src: string; name: string; thumbnail?: boolean; attachment: Attachment }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  if (failed) return <small className="warning">{t("Image preview unavailable or expired.")}</small>;
  // Only preview backend-validated uploads, not unvalidated blob/data URLs.
  // eslint-disable-next-line @next/next/no-img-element
  return <img className={thumbnail ? "upload-thumbnail" : "message-image"} src={src} alt={name}
    width={attachment.reading_scope?.width} height={attachment.reading_scope?.height} loading="lazy" onError={() => setFailed(true)} />;
}

export function AttachmentScope({ item }: { item: Attachment }) {
  const { t } = useI18n();
  const scope = item.reading_scope;
  if (!scope || !Object.keys(scope).length) return <small>{t("Reading details unavailable for this older attachment.")}</small>;
  return <small className="attachment-scope">{scope.width && scope.height
    ? t("Image: {p0} × {p1} → {p2} × {p3}; sanitized first frame.", { p0: scope.original_width, p1: scope.original_height, p2: scope.width, p3: scope.height })
    : t("Prepared excerpt: {p0} characters. {p1}", { p0: scope.excerpt_chars, p1: t(scope.truncated ? "Truncated." : "Not truncated.") })}
    {" "}{t("Original / stored: {p0} / {p1} bytes.", { p0: scope.original_bytes, p1: scope.stored_bytes })}
  </small>;
}

export function UploadList({ uploads, remove, cancel, retry, disabled, sessionId, status }: { uploads: Upload[]; remove: (id: string) => Promise<void>; cancel: (id: string) => void; retry: (id: string, file: File) => Promise<void>; disabled: boolean; sessionId: string; status?: AttachmentStatus }) {
  const { t, date } = useI18n();
  return <ul className="upload-list">{uploads.map(item => <li key={item.id}>
    {item.attachment?.media_type.startsWith("image/") && <UploadedImage src={attachmentPath(sessionId, item.id)} name={item.file.name} thumbnail attachment={item.attachment} />}
    <span className="upload-name">{item.file.name}<small role="status">{Math.ceil(item.file.size / 1024)} {"KB"} · {item.uploading
      ? item.progress === 1 ? t("Uploaded; validating and saving…") : t("Uploading…")
      : item.error ? t("Upload failed") : t("Ready")}</small>
      {item.uploading && <progress aria-label={t("Upload progress for {p0}", { p0: item.file.name })} max={1} value={item.progress ?? 0} />}
      {item.attachment && <AttachmentScope item={item.attachment} />}
      {item.attachment && <small>{t("Expires")} {date(item.attachment.expires_at)}</small>}
      {item.attachment && status?.items.some(row => row.id === item.id && row.available === false)
        && <small className="warning">{t("Attachment unavailable. Remove it and attach the file again.")}</small>}
    </span>
    {item.error && <span className="warning" role="alert">{t(item.error)}</span>}
    {item.error && <button type="button" disabled={disabled || item.uploading} onClick={() => void retry(item.id, item.file)}>{t("Retry")}</button>}
    {item.uploading ? <button type="button" disabled={disabled} onClick={() => cancel(item.id)}>{t("Cancel upload")}</button>
      : <button type="button" disabled={disabled} onClick={() => void remove(item.id)} aria-label={t("Remove attachment {p0}", { p0: item.file.name })}>{"×"}</button>}
  </li>)}</ul>;
}

export function MessageAttachments({ items, sessionId, status }: { items: Attachment[]; sessionId: string; status?: AttachmentStatus }) {
  const { t, date } = useI18n();
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => clearInterval(timer);
  }, []);
  if (!items.length) return null;
  return <div className="message-attachments">{items.map(item => {
    const url = attachmentPath(sessionId, item.id);
    const expired = item.expires_at <= now || status?.items.some(row => row.id === item.id && row.available === false);
    return <figure className="attachment-card" key={item.id}>
      {!expired && item.media_type.startsWith("image/") && <a className="message-image-link" href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
        <UploadedImage src={url} name={item.name} attachment={item} />
      </a>}
      <figcaption>{expired ? <span>{item.name} · {t("Attachment unavailable or expired")}</span> : <a href={`${url}?download=true`} download={item.name}>{item.name}</a>}
        <small>{Math.ceil(item.size / 1024)} {"KB"} · {t("Expires")} {date(item.expires_at)}</small>
        <small>{t(item.model_context === "image" ? "Image input for the model" : "Text excerpt for the model")}</small>
        <AttachmentScope item={item} />
      </figcaption>
    </figure>;
  })}</div>;
}
