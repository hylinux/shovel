import type { Upload } from "./chat-attachments";
import type { MessageFormat } from "./message-format";

type DraftState = {
  content: string; format: MessageFormat; preview: boolean;
  codeLanguage?: string;
  uploads: Upload[]; error: string; notice: string;
  selection?: { start: number; end: number; direction: "forward" | "backward" | "none" };
};

export class SessionDraft {
  private state: DraftState = { content: "", format: "markdown", preview: false, uploads: [], error: "", notice: "" };
  private listeners = new Set<() => void>();
  private disposed = false;
  readonly requests = new Map<string, AbortController>();
  private readonly changed: () => void;
  constructor(changed: () => void = () => {}) { this.changed = changed; }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  update = (patch: Partial<DraftState> | ((state: DraftState) => Partial<DraftState>)) => {
    if (this.disposed) return;
    this.state = { ...this.state, ...(typeof patch === "function" ? patch(this.state) : patch) };
    this.listeners.forEach(listener => listener());
    this.changed();
  };
  clearAfterSend(sent: DraftState) {
    if (this.state !== sent) return;
    this.update({ content: "", preview: false, uploads: [], error: "", notice: "" });
  }
  dispose() {
    this.disposed = true;
    this.requests.forEach(request => request.abort());
    this.requests.clear();
    this.listeners.clear();
    this.state = { content: "", format: "markdown", preview: false, uploads: [], error: "", notice: "" };
    this.changed();
  }
}

export class SessionDrafts {
  private drafts = new Map<string, SessionDraft>();
  private listeners = new Set<() => void>();
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  private changed = () => { this.listeners.forEach(listener => listener()); };
  hasUnsaved = () => [...this.drafts.values()].some(draft => {
    const state = draft.getSnapshot();
    return state.content.length > 0 || state.uploads.length > 0 || draft.requests.size > 0;
  });
  private key(workspace: string, session: string) { return JSON.stringify([workspace, session]); }
  get(workspace: string, session: string) {
    const key = this.key(workspace, session);
    let draft = this.drafts.get(key);
    if (!draft) { draft = new SessionDraft(this.changed); this.drafts.set(key, draft); }
    return draft;
  }
  delete(workspace: string, session: string) {
    const key = this.key(workspace, session);
    this.drafts.get(key)?.dispose();
    this.drafts.delete(key);
  }
}

export function protectDrafts(drafts: SessionDrafts, target: Pick<Window, "addEventListener" | "removeEventListener">) {
  const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
  let listening = false;
  const update = () => {
    const needed = drafts.hasUnsaved();
    if (needed === listening) return;
    if (needed) target.addEventListener("beforeunload", warn);
    else target.removeEventListener("beforeunload", warn);
    listening = needed;
  };
  update();
  const unsubscribe = drafts.subscribe(update);
  return () => { unsubscribe(); target.removeEventListener("beforeunload", warn); };
}
