export type MemoryKind = "semantic" | "episodic" | "procedural";
export type MemoryScope = "user" | "workspace" | "agent" | "session";
export type MemoryInput = {
  text: string; kind: MemoryKind; scope: MemoryScope; session_id: string;
  subject: string; predicate: string; object: string; topic: string;
  importance: number; expires_at: number | null; occurred_at: number | null;
  time_precision?: "day" | "instant";
};
export type Memory = {
  id: string; memory: string; kind: MemoryKind;
  state: "active" | "candidate" | "superseded" | "expired" | "deleted" | "rejected";
  scope: MemoryScope; session_id?: string | null; revision?: number;
  superseded_by?: string | null;
  created_at?: number | null; expires_at?: number | null; occurred_at?: number | null;
  episode_time_source?: string | null; source?: string; confidence?: number; importance?: number;
  episode_time_precision?: "day" | "instant" | "unknown";
  subject?: string; predicate?: string; object?: string; topic?: string;
  provenance?: { evidence?: string; source_kind: string; conversation_id: string; message_id: string; asserted_at?: number | null }[];
};
export type WorkingMemory = {
  task_id: string; revision: number; goal: string; constraints: string[]; decisions: string[];
  plan: string[]; blockers: string[]; next_step: string; evidence_message_ids: string[];
  state: "active" | "completed" | "archived" | "expired"; expires_at: number | null;
  updated_at?: number | null; last_user_message_id?: string | null; last_assistant_message_id?: string | null;
};
export type WorkingInput = Omit<WorkingMemory, "task_id" | "revision" | "updated_at" | "last_user_message_id" | "last_assistant_message_id" | "state"> & { expected_revision: number; state: "active" | "completed" | "archived" };

export function memoryInput(item?: Memory, sessionId = ""): MemoryInput {
  return {
    text: item?.memory ?? "", kind: item?.kind ?? "semantic", scope: item?.scope ?? "agent",
    session_id: item?.scope === "session" ? item.session_id ?? sessionId : "",
    subject: item?.subject || "user",
    predicate: item?.predicate || (item && item.kind !== "semantic" ? "knows_about" : "prefers"),
    object: item?.object ?? "",
    topic: item?.topic ?? "", importance: item?.importance ?? 0.5,
    expires_at: item?.expires_at ?? null,
    occurred_at: item?.kind === "episodic" && item.episode_time_source === "explicit" ? item.occurred_at ?? null : null,
    ...(item?.kind === "episodic" && item.episode_time_source === "explicit" && item.episode_time_precision === "day"
      ? { time_precision: "day" as const } : {}),
  };
}

export function memoryPayload(draft: MemoryInput, original?: Memory): MemoryInput {
  const kind = original?.kind ?? draft.kind;
  const scope = original?.scope ?? draft.scope;
  const sessionId = original ? original.session_id : draft.session_id;
  if (!draft.text.trim()) throw new Error("Memory text is required.");
  if (scope === "session" && !sessionId) throw new Error("Choose a Session for session-scoped Memory.");
  const { time_precision, ...input } = draft;
  const keepDay = kind === "episodic" && time_precision === "day" && (!original || draft.occurred_at === original.occurred_at);
  return { ...input, text: draft.text.trim(), kind, scope, session_id: scope === "session" ? sessionId! : "",
    occurred_at: kind === "episodic" ? draft.occurred_at : null,
    ...(keepDay ? { time_precision: "day" as const } : {}) };
}

export function newWorkingTask(taskId: string): WorkingMemory {
  return { task_id: taskId, revision: 0, goal: "", constraints: [], decisions: [], plan: [], blockers: [],
    next_step: "", evidence_message_ids: [], state: "active", expires_at: null };
}

export function workingPayload(task: WorkingMemory): WorkingInput {
  const { goal, next_step, state, expires_at } = task;
  if (state === "expired") throw new Error("Expired working tasks cannot be edited. Create a new task.");
  const [constraints, decisions, plan, blockers, evidence_message_ids] =
    [task.constraints, task.decisions, task.plan, task.blockers, task.evidence_message_ids]
      .map(entries => entries.filter(entry => entry.trim().length > 0));
  if (!goal.trim()) throw new Error("A working task needs a goal.");
  if (goal.length > 1000 || next_step.length > 1000) throw new Error("Goal and next step must be at most 1000 characters.");
  for (const entries of [constraints, decisions, plan, blockers, evidence_message_ids]) {
    if (entries.length > 20 || entries.some(entry => entry.length > 500)) {
      throw new Error("Use at most 20 entries per list, with at most 500 characters each.");
    }
  }
  if (evidence_message_ids.some(id => id.length > 200)) throw new Error("Evidence message IDs must be at most 200 characters.");
  return { expected_revision: task.revision, goal, constraints, decisions, plan, blockers, next_step, evidence_message_ids, state, expires_at };
}

export function selectedWorkingTask(items: WorkingMemory[], taskId: string): string | null {
  return items.find(item => item.task_id === taskId && item.state === "active")?.task_id ?? null;
}

export function memoryExtractionNotice(result: Record<string, unknown>): string {
  if (typeof result.candidates === "number" && result.candidates > 0) return "saved awaiting review";
  return result.memories === 0 ? "no durable facts found" : "saved in SQLite";
}

export function lines(value: string): string[] {
  return value.split(/\r?\n/);
}

export type WorkingTransport = <T>(path: string, options?: RequestInit) => Promise<T>;

// Working notes are CAS writes, not durable queued Operations. Never replay a lost response.
export async function writeWorkingTask(transport: WorkingTransport, base: string, task: WorkingMemory, remove = false) {
  const path = `${base}/${encodeURIComponent(task.task_id)}`;
  try {
    const result = await transport<WorkingMemory | { deleted: boolean }>(
      remove ? `${path}?revision=${task.revision}` : path,
      remove ? { method: "DELETE" } : { method: "PUT", body: JSON.stringify(workingPayload(task)) },
    );
    return { ok: true as const, result };
  } catch (error) {
    const status = error && typeof error === "object" && "status" in error ? error.status : undefined;
    if (typeof status === "number" && status >= 400 && status < 500 && status !== 409) {
      return { ok: false as const, error, needsReload: false, rechecked: false, reconciliationError: undefined, items: undefined };
    }
    try {
      const items = (await transport<{ items: WorkingMemory[] }>(base)).items;
      return { ok: false as const, error, needsReload: true, rechecked: true, reconciliationError: undefined, items };
    } catch (reconciliationError) {
      return { ok: false as const, error, needsReload: true, rechecked: false, reconciliationError, items: undefined };
    }
  }
}
