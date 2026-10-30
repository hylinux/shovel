"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { forgetPendingRequest, makePendingRequest, pendingRequests, savePendingRequest, type PendingRequest } from "./request-recovery";
import { isPlatformRun } from "./platform-types";
import { runAgentMessage, type AgentMessageInput } from "./agent-client";
import { mergeSessionHistory } from "./session-history";
import { startQueryPolling } from "./query-polling";
import { invalidateReads, sharedRead } from "./read-requests";

export type Status = { service: string; workspace_id: string; embedding_configured: boolean; model_configured: boolean; model_name: string; memory_configured: boolean; memory?: { provider: string; index_state: string; pending_index_operations: number; last_error: string | null }; warmup?: Record<string, { state: string; elapsed_ms?: number; error_type?: string }>; chat_capacity?: { limit: number; running: number; waiting: number } };
export type UnixSeconds = number;
export type Resource = { id: string; name: string; connector_kind: string; state: string; config: { root_path?: string; store_id?: string; folder_ids?: string[]; include_subfolders?: boolean }; description?: string; authority?: string; sensitivity?: string; state_reason?: string; last_verified_at?: UnixSeconds | null };
export type OutlookStore = { id: string; name: string };
export type OutlookFolder = { id: string; name: string; path: string };
export type Connector = { kind: string; display_name: string; description: string; config_schema: unknown; capabilities: string[]; supported_modes: string[]; requires_identity: boolean };
export type Operation = { id: string; kind: string; state: string; resource_id: string | null; job_id: string | null; result: Record<string, unknown>; error: string | null; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null };
export type Job = { id: string; resource_id: string; resource_name: string; scan_mode: string; state: string; discovered: number; skipped_unchanged: number; fetched: number; parsed: number; chunked: number; embedded: number; failed_docs: number; chunks_written: number; bytes_read: number; current_stage: string; progress: number; error_detail?: string; queued_at: UnixSeconds; started_at?: UnixSeconds | null; finished_at?: UnixSeconds | null; is_cancel_requested: boolean };
export type JobEvent = { id: string; logged_at: UnixSeconds; level: string; stage: string; code: string; message: string; doc_uri?: string };
export type Session = { id: string; title: string; created_at: UnixSeconds; updated_at: UnixSeconds };
export type Citation = { index?: number; chunk_id: string; resource_id: string; resource_name: string; doc_uri: string; title: string; text: string; score: number; email?: { sender?: string; to?: string; sent_at?: UnixSeconds | null; received_at?: UnixSeconds | null; folder_path?: string } };
export type Message = { id: string; conversation_id: string; role: "user" | "assistant"; content: string; state: string; citations: Citation[]; warnings: string[]; created_at: UnixSeconds; memory_operation_id?: string | null };
export type Memory = { id: string; memory: string; kind?: string; state?: string; scope?: string; revision?: number; created_at?: UnixSeconds | null; provenance?: { source_kind: string; conversation_id: string; message_id: string }[] };
export type Items<T> = { items: T[] };
export type Profile = { id: string; name: string; scan_mode: string; is_enabled: boolean; include: string[]; exclude: string[]; max_file_bytes: number; time_window_days: number | null; pipeline: Record<string, unknown> };
export type MessagePage = { items: (Message & { sequence: number })[]; next_before: number | null };
export type MCPConfiguration = { path: string; content: string; revision: string; exists: boolean; items: { name: string; transport: "stdio" | "http"; enabled: boolean }[]; validation_error: string | null; agent_loaded: boolean; agent_state: "loaded" | "not_loaded" | "restarting" | "restart_failed"; restart_required: boolean };
export type AgentInstructions = { path: string; content: string; revision: string; exists: boolean; builtin_instructions: string; max_length: number; validation_error: string | null; applied: boolean; agent_state: MCPConfiguration["agent_state"]; restart_required: boolean };

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

export class ApiError extends Error {
  constructor(message: string, public readonly status?: number) { super(message); }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  const read = !options.body && (options.method || "GET").toUpperCase() === "GET";
  try {
    if (!read) invalidateReads();
    response = await (read ? sharedRead : fetch)(`/api/v1${path}`, {
      ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError("Cannot reach Shovel. Check that the backend is running, then retry.");
  } finally {
    if (!read) invalidateReads();
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

export function useQuery<T>(path: string | null, interval = 0, shouldPoll?: (result: T) => boolean) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  const [revision, refresh] = useReducer((value: number) => value + 1, 0);
  useEffect(() => {
    if (!path) return;
    return startQueryPolling(async (signal) => {
      try {
        const result = await api<T>(path, { signal });
        if (!signal.aborted) {
          setData(result); setError("");
          return shouldPoll?.(result);
        }
      } catch (error) {
        if (!signal.aborted) setError(errorText(error));
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    }, interval);
  }, [path, interval, revision, shouldPoll]);
  return { data, error, loading, refresh };
}

export function useSessionHistory(revision: number) {
  const [data, setData] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [refreshRevision, refresh] = useReducer((value: number) => value + 1, 0);
  const controller = useRef<AbortController | null>(null);
  const nextOffset = useRef(0);
  const load = useCallback((offset: number) => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    void api<Items<Session>>(`/conversations?limit=100&offset=${offset}`, { signal: request.signal }).then((page) => {
      if (!request.signal.aborted) {
        setData((current) => mergeSessionHistory(offset === 0 ? [] : current, page.items));
        nextOffset.current = offset + page.items.length;
        setHasMore(page.items.length === 100);
        setError("");
      }
    }, (error: unknown) => {
      if (!request.signal.aborted) setError(errorText(error));
    }).finally(() => {
      if (!request.signal.aborted) setLoading(false);
      if (controller.current === request) controller.current = null;
    });
  }, []);
  useEffect(() => {
    void load(0);
    return () => controller.current?.abort();
  }, [load, revision, refreshRevision]);
  const loadMore = useCallback(() => {
    if (!controller.current) { setLoading(true); void load(nextOffset.current); }
  }, [load]);
  return { data, loading, error, hasMore, refresh, loadMore };
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
    const platformRun = path === "/platform/commands" && isPlatformRun(body);
    const durable = platformRun || path.startsWith("/resources") || path === "/index/cleanup" || path === "/history/prune"
      || path === "/memory/index/rebuild" || (path.startsWith("/memories") && method !== "DELETE");
    let submitted = false;
    try {
      if (durable) {
        const status = await api<Status>("/status", { signal: request.signal });
        if (!pending.current || pending.current.path !== path || pending.current.method !== method
          || pending.current.body !== serialized || pending.current.workspace !== status.workspace_id) {
          pending.current = (platformRun ? pendingRequests(status.workspace_id).find((row) =>
            row.path === path && row.method === method && row.body === serialized) : undefined)
            || await makePendingRequest(status.workspace_id, path, method, serialized);
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
        ? platformRun
          ? ` Outcome unknown. Request key: ${pending.current.key}. Find it in Activity before submitting again.`
          : ` Outcome unknown. Recovery ID: ${pending.current.id}. Find it in Background tasks before submitting again.` : ""));
    } finally {
      if (!request.signal.aborted) setBusy(false);
      controller.current = null;
    }
  }, []);
  return { run, busy, error, notice, setNotice };
}

export function useAgentAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [tool, setTool] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const run = useCallback(async (sessionId: string, input: AgentMessageInput, runId: string): Promise<boolean> => {
    if (controller.current) return false;
    const request = new AbortController();
    controller.current = request;
    setBusy(true); setError(""); setText(""); setTool("");
    try {
      await runAgentMessage("/api/v1/agent", sessionId, input, request.signal, setText, setTool, runId);
      return true;
    } catch (error) {
      if (!request.signal.aborted) setError(`${errorText(error)} Check session history before resending.`);
      return false;
    } finally {
      invalidateReads();
      if (!request.signal.aborted) { setBusy(false); setText(""); setTool(""); }
      controller.current = null;
    }
  }, []);
  return { run, busy, error, text, tool };
}

export function date(value?: UnixSeconds | null) {
  if (value == null || !Number.isFinite(value)) return "—";
  const instant = new Date(value * 1000);
  return Number.isNaN(instant.getTime()) ? "—" : instant.toLocaleString();
}
export function segment(value: string) { return encodeURIComponent(value); }
export function active(state: string) { return state === "queued" || state === "running"; }
