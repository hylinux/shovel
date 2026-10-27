"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { forgetPendingRequest, makePendingRequest, savePendingRequest, type PendingRequest } from "./request-recovery";

export type Status = { service: string; workspace_id: string; embedding_configured: boolean; model_configured: boolean; model_name: string; memory_configured: boolean };
export type UnixSeconds = number;
export type Resource = { id: string; name: string; connector_kind: string; state: string; config: { root_path?: string; store_id?: string; folder_ids?: string[]; include_subfolders?: boolean }; description?: string; authority?: string; sensitivity?: string; state_reason?: string; last_verified_at?: UnixSeconds | null };
export type OutlookStore = { id: string; name: string };
export type OutlookFolder = { id: string; name: string; path: string };
export type Connector = { kind: string; display_name: string; description: string; config_schema: unknown; capabilities: string[]; supported_modes: string[]; requires_identity: boolean };
export type Operation = { id: string; kind: string; state: string; resource_id: string | null; job_id: string | null; result: Record<string, unknown>; error: string | null; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null };
export type Job = { id: string; resource_id: string; resource_name: string; scan_mode: string; state: string; discovered: number; skipped_unchanged: number; fetched: number; parsed: number; chunked: number; embedded: number; failed_docs: number; chunks_written: number; bytes_read: number; current_stage: string; progress: number; error_detail?: string; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null; is_cancel_requested: boolean };
export type JobEvent = { id: string; logged_at: UnixSeconds; level: string; stage: string; code: string; message: string; doc_uri?: string };
export type Conversation = { id: string; title: string; created_at: UnixSeconds; updated_at: UnixSeconds };
export type Citation = { index?: number; chunk_id: string; resource_id: string; resource_name: string; doc_uri: string; title: string; text: string; score: number; email?: { sender?: string; to?: string; sent_at?: UnixSeconds | null; received_at?: UnixSeconds | null; folder_path?: string } };
export type Message = { id: string; conversation_id: string; role: "user" | "assistant"; content: string; state: string; citations: Citation[]; warnings: string[]; created_at: UnixSeconds };
export type Memory = { id: string; memory: string; created_at?: UnixSeconds | null };
export type Items<T> = { items: T[] };
export type Profile = { id: string; name: string; scan_mode: string; is_enabled: boolean; include: string[]; exclude: string[]; max_file_bytes: number; time_window_days: number | null; pipeline: Record<string, unknown> };
export type MessagePage = { items: (Message & { sequence: number })[]; next_before: number | null };

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

export class ApiError extends Error {
  constructor(message: string, public readonly status?: number) { super(message); }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError("Cannot reach Shovel. Check that the backend is running, then retry.");
  }
  let body;
  try { body = await response.json(); }
  catch { throw new ApiError("Shovel returned an invalid response; the operation outcome may be unknown."); }
  if (!response.ok) {
    const detail = Array.isArray(body?.detail)
      ? body.detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.join(".") || "Input"}: ${item.msg || "Invalid value"}`).join("; ")
      : typeof body?.detail === "string" ? body.detail : null;
    throw new ApiError([body?.error?.message || detail || `Request failed (${response.status}).`, body?.error?.hint].filter(Boolean).join(" "), response.status);
  }
  return body as T;
}

// Schedule the next poll only after the previous request finishes.
export function useQuery<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  const [revision, refresh] = useReducer((value: number) => value + 1, 0);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const result = await api<T>(path!, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(result); setError(""); }
      } catch (error) {
        if (!controller.signal.aborted) setError(errorText(error));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (interval) timer = setTimeout(load, interval);
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval, revision]);
  return { data, error, loading, refresh };
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const controller = useRef<AbortController | null>(null);
  const pending = useRef<PendingRequest | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const run = useCallback(async <T,>(path: string, body?: unknown, method = "POST"): Promise<T | undefined> => {
    if (controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError(""); setNotice("");
    const serialized = body === undefined ? undefined : JSON.stringify(body);
    const durable = path.startsWith("/resources") || path === "/index/cleanup" || path === "/history/prune";
    let submitted = false;
    try {
      if (durable) {
        const status = await api<Status>("/status", { signal: request.signal });
        if (!pending.current || pending.current.path !== path || pending.current.method !== method
          || pending.current.body !== serialized || pending.current.workspace !== status.workspace_id) {
          pending.current = await makePendingRequest(status.workspace_id, path, method, serialized);
        }
        savePendingRequest(pending.current);
      }
      submitted = true;
      const result = await api<T>(path, {
        method, body: serialized, signal: request.signal,
        headers: durable && pending.current ? { "Idempotency-Key": pending.current.key } : {},
      });
      if (durable && pending.current) { forgetPendingRequest(pending.current.key); pending.current = null; }
      return result;
    } catch (error) {
      if (durable && pending.current && error instanceof ApiError && error.status
        && error.status >= 400 && error.status < 500) {
        forgetPendingRequest(pending.current.key); pending.current = null;
      }
      if (!request.signal.aborted) setError(errorText(error) + (submitted && durable && pending.current
        ? ` Outcome unknown. Recovery ID: ${pending.current.id}. Find it in Background tasks before submitting again.` : ""));
    } finally {
      if (!request.signal.aborted) setBusy(false);
      controller.current = null;
    }
  }, []);
  return { run, busy, error, notice, setNotice };
}

export function date(value?: UnixSeconds | null) {
  if (value == null || !Number.isFinite(value)) return "—";
  const instant = new Date(value * 1000);
  return Number.isNaN(instant.getTime()) ? "—" : instant.toLocaleString();
}
export function segment(value: string) { return encodeURIComponent(value); }
export function active(state: string) { return state === "queued" || state === "running"; }
