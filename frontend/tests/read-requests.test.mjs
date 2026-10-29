import assert from "node:assert/strict";
import test from "node:test";
import { invalidateReads, sharedRead } from "../app/read-requests.ts";

function setup(t) {
  const requests = [];
  t.mock.method(globalThis, "fetch", (_path, options) => new Promise((resolve, reject) => {
    requests.push({ signal: options.signal, resolve, reject });
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  invalidateReads();
  t.after(invalidateReads);
  return requests;
}

test("Concurrent readers share transport but receive independent response bodies", async (t) => {
  const calls = setup(t);
  const first = sharedRead("/shared");
  const second = sharedRead("/shared");
  assert.equal(calls.length, 1);
  calls[0].resolve(Response.json({ items: [1] }));
  assert.deepEqual(await (await first).json(), { items: [1] });
  assert.deepEqual(await (await second).json(), { items: [1] });
  const fresh = sharedRead("/shared");
  assert.equal(calls.length, 2);
  calls[1].resolve(Response.json({ items: [2] }));
  assert.deepEqual(await (await fresh).json(), { items: [2] });
});

test("Strict Mode abort and resubscribe reuse an in-flight read without cancelling a live reader", async (t) => {
  const calls = setup(t);
  const cancelled = new AbortController();
  const first = sharedRead("/strict", { signal: cancelled.signal });
  cancelled.abort();
  const rejection = assert.rejects(first, { name: "AbortError" });
  const second = sharedRead("/strict", { signal: new AbortController().signal });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal.aborted, false);
  calls[0].resolve(Response.json({ ready: true }));
  assert.deepEqual(await (await second).json(), { ready: true });
  await rejection;
});

test("The last cancelled reader aborts transport and a failed read is never retained", async (t) => {
  const calls = setup(t);
  const controller = new AbortController();
  const read = sharedRead("/cancel", { signal: controller.signal });
  controller.abort();
  await assert.rejects(read, { name: "AbortError" });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(calls[0].signal.aborted, true);
  const retry = sharedRead("/cancel");
  calls[1].reject(new Error("network"));
  await assert.rejects(retry, /network/);
  const again = sharedRead("/cancel");
  calls[2].resolve(Response.json({ ok: true }));
  await again;
});

test("Mutation invalidation and differing headers keep reads separate", async (t) => {
  const calls = setup(t);
  const old = sharedRead("/revision");
  invalidateReads();
  const fresh = sharedRead("/revision");
  const other = sharedRead("/revision", { headers: { "X-Test": "other" } });
  assert.equal(calls.length, 3);
  calls.forEach((request, index) => request.resolve(Response.json({ revision: index })));
  assert.deepEqual(await (await old).json(), { revision: 0 });
  assert.deepEqual(await (await fresh).json(), { revision: 1 });
  assert.deepEqual(await (await other).json(), { revision: 2 });
});

test("Resolving headers does not abort a body that is still streaming", async (t) => {
  const calls = setup(t);
  let release;
  const body = new ReadableStream({
    start(controller) { release = () => { controller.enqueue(new TextEncoder().encode('{"ok":true}')); controller.close(); }; },
  });
  const read = sharedRead("/slow-body");
  calls[0].resolve(new Response(body));
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(calls[0].signal.aborted, false);
  release();
  const response = await read;
  assert.deepEqual(await response.json(), { ok: true });
});
