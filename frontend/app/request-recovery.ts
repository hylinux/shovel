export type PendingRequest = {
  key: string;
  id: string;
  workspace: string;
  path: string;
  method: string;
  body?: string;
};

const prefix = "shovel.request.";

export async function makePendingRequest(
  workspace: string, path: string, method: string, body?: string,
): Promise<PendingRequest> {
  const key = crypto.randomUUID();
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  const suffix = Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return { key, id: `op_request_${suffix}`, workspace, path, method, body };
}

function isPendingRequest(value: unknown): value is PendingRequest {
  return typeof value === "object" && value !== null
    && "key" in value && typeof value.key === "string"
    && "id" in value && typeof value.id === "string"
    && "workspace" in value && typeof value.workspace === "string"
    && "path" in value && typeof value.path === "string"
    && (/^\/resources(?:\/|$)/.test(value.path) || value.path === "/index/cleanup" || value.path === "/history/prune"
      || /^\/memories(?:\/|$)/.test(value.path) || value.path === "/memory/index/rebuild")
    && "method" in value && typeof value.method === "string" && ["POST", "PUT", "PATCH", "DELETE"].includes(value.method)
    && (!("body" in value) || typeof value.body === "string");
}

export function savePendingRequest(request: PendingRequest) {
  localStorage.setItem(prefix + request.key, JSON.stringify(request));
}

export function forgetPendingRequest(key: string) {
  localStorage.removeItem(prefix + key);
}

export function pendingRequests(workspace: string): PendingRequest[] {
  const requests: PendingRequest[] = [];
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index);
    if (!key?.startsWith(prefix)) continue;
    const raw = localStorage.getItem(key);
    if (raw === null) continue;
    const value: unknown = JSON.parse(raw);
    if (!isPendingRequest(value)) throw new Error("An invalid recovery receipt was found in browser storage.");
    if (value.workspace === workspace) requests.push(value);
  }
  return requests;
}
