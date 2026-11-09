import type { Upload } from "./chat-attachments";
import type { MessageFormat } from "./message-format";

type DraftState = {
  content: string; format: MessageFormat; preview: boolean;
  uploads: Upload[]; error: string; notice: string;
};

export class SessionDraft {
  private state: DraftState = { content: "", format: "markdown", preview: false, uploads: [], error: "", notice: "" };
  private listeners = new Set<() => void>();
  private disposed = false;
  readonly requests = new Map<string, AbortController>();
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  update = (patch: Partial<DraftState> | ((state: DraftState) => Partial<DraftState>)) => {
    if (this.disposed) return;
    this.state = { ...this.state, ...(typeof patch === "function" ? patch(this.state) : patch) };
    this.listeners.forEach(listener => listener());
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
  }
}

export class SessionDrafts {
  private drafts = new Map<string, SessionDraft>();
  private key(workspace: string, session: string) { return JSON.stringify([workspace, session]); }
  get(workspace: string, session: string) {
    const key = this.key(workspace, session);
    let draft = this.drafts.get(key);
    if (!draft) { draft = new SessionDraft(); this.drafts.set(key, draft); }
    return draft;
  }
  delete(workspace: string, session: string) {
    const key = this.key(workspace, session);
    this.drafts.get(key)?.dispose();
    this.drafts.delete(key);
  }
}
