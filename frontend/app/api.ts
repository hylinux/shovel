"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";

export type Status = { service: string; embedding_configured: boolean; model_configured: boolean; model_name: string; memory_configured: boolean };
export type UnixSeconds = number;
export type Resource = { id: string; name: string; connector_kind: string; state: string; config: { root_path?: string }; description?: string; authority?: string; sensitivity?: string; state_reason?: string; last_verified_at?: UnixSeconds | null };
export type Connector = { kind: string; display_name: string; description: string; config_schema: unknown; capabilities: string[]; supported_modes: string[]; requires_identity: boolean };
export type Operation = { id: string; kind: string; state: string; resource_id: string | null; job_id: string | null; result: Record<string, unknown>; error: string | null; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null };
export type Job = { id: string; resource_id: string; resource_name: string; scan_mode: string; state: string; discovered: number; skipped_unchanged: number; fetched: number; parsed: number; chunked: number; embedded: number; failed_docs: number; chunks_written: number; bytes_read: number; current_stage: string; progress: number; error_detail?: string; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null; is_cancel_requested: boolean };
export type JobEvent = { id: string; logged_at: UnixSeconds; level: string; stage: string; code: string; message: string; doc_uri?: string };
export type Conversation = { id: string; title: string; created_at: UnixSeconds; updated_at: UnixSeconds };
export type Citation = { index?: number; chunk_id: string; resource_id: string; resource_name: string; doc_uri: string; title: string; text: string; score: number };
export type Message = { id: string; conversation_id: string; role: "user" | "assistant"; content: string; state: string; citations: Citation[]; warnings: string[]; created_at: UnixSeconds };
export type Memory = { id: string; memory: string; created_at?: UnixSeconds | null };
export type Items<T> = { items: T[] };

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
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
    throw new Error("Cannot reach Shovel. Check that the backend is running, then retry.");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = Array.isArray(body?.detail)
      ? body.detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.join(".") || "Input"}: ${item.msg || "Invalid value"}`).join("; ")
      : typeof body?.detail === "string" ? body.detail : null;
    throw new Error([body?.error?.message || detail || `Request failed (${response.status}).`, body?.error?.hint].filter(Boolean).join(" "));
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
  useEffect(() => () => controller.current?.abort(), []);
  const run = useCallback(async <T,>(path: string, body?: unknown, method = "POST"): Promise<T | undefined> => {
    if (controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError(""); setNotice("");
    try {
      return await api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body), signal: request.signal });
    } catch (error) {
      if (!request.signal.aborted) setError(errorText(error));
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
