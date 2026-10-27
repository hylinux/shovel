import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { forgetPendingRequest, makePendingRequest, pendingRequests, savePendingRequest } from "../app/request-recovery.ts";

test("recovery receipts survive reload, match backend IDs and stay workspace scoped", async () => {
  const values = new Map();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      get length() { return values.size; },
      key(index) { return [...values.keys()][index] ?? null; },
      getItem(key) { return values.get(key) ?? null; },
      setItem(key, value) { values.set(key, value); },
      removeItem(key) { values.delete(key); },
    },
  });
  const request = await makePendingRequest("workspace-a", "/resources/notes/scans", "POST", '{"embed":false}');
  assert.equal(request.id, "op_request_" + createHash("sha256").update(request.key).digest("hex"));
  savePendingRequest(request);
  assert.deepEqual(pendingRequests("workspace-a"), [request]);
  assert.deepEqual(pendingRequests("workspace-b"), []);
  forgetPendingRequest(request.key);
  assert.deepEqual(pendingRequests("workspace-a"), []);
  for (const [path, method] of [
    ["/resources/notes/profiles/text", "PUT"],
    ["/resources/notes/profiles/text", "DELETE"],
    ["/history/prune", "POST"],
  ]) {
    const stored = await makePendingRequest("workspace-a", path, method, "{}");
    savePendingRequest(stored);
    assert.deepEqual(pendingRequests("workspace-a"), [stored]);
    forgetPendingRequest(stored.key);
  }
  values.set("shovel.request.bad-method", JSON.stringify({ ...request, method: 123 }));
  assert.throws(() => pendingRequests("workspace-a"), /invalid recovery receipt/);
  values.delete("shovel.request.bad-method");
  values.set("shovel.request.broken", JSON.stringify({ path: "/resources" }));
  assert.throws(() => pendingRequests("workspace-a"), /invalid recovery receipt/);
});
