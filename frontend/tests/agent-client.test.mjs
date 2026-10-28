import assert from "node:assert/strict";
import test from "node:test";
import { runAgentMessage } from "../app/agent-client.ts";

const input = { content: "Question", resource_ids: ["resource"], memory_enabled: false, search_mode: "keyword" };

function response(input, success = true) {
  const events = [
    { type: "RUN_STARTED", threadId: input.threadId, runId: input.runId },
    { type: "TEXT_MESSAGE_START", messageId: "answer", role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "Streamed answer" },
    { type: "TEXT_MESSAGE_END", messageId: "answer" },
    success
      ? { type: "RUN_FINISHED", threadId: input.threadId, runId: input.runId }
      : { type: "RUN_ERROR", message: "Citation validation failed" },
  ];
  return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), {
    headers: { "content-type": "text/event-stream" },
  });
}

test("AG-UI client sends scoped requests and renders streaming text", async (t) => {
  let captured;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "/api/v1/agent");
    captured = JSON.parse(options.body);
    return response(captured);
  });
  const texts = [];
  await runAgentMessage("/api/v1/agent", "conversation", input, new AbortController().signal,
    (text) => texts.push(text), () => {});
  assert.equal(captured.threadId, "conversation");
  assert.deepEqual(captured.messages.map(({ role, content }) => ({ role, content })), [{ role: "user", content: "Question" }]);
  assert.deepEqual(captured.forwardedProps, { resource_ids: ["resource"], memory_enabled: false, search_mode: "keyword" });
  assert.deepEqual(texts, ["Streamed answer"]);
});

test("AG-UI terminal failure is not treated as a saved answer", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url, options) => response(JSON.parse(options.body), false));
  await assert.rejects(runAgentMessage("/api/v1/agent", "conversation", input, new AbortController().signal,
    () => {}, () => {}), /Citation validation failed/);
});

test("AG-UI requests respect cancellation before submission", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", () => { throw new Error("Must not send"); });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(runAgentMessage("/api/v1/agent", "conversation", input, controller.signal,
    () => {}, () => {}), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 0);
});

test("AG-UI interrupted connections do not claim persistence", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    const input = JSON.parse(options.body);
    return new Response(`data: ${JSON.stringify({ type: "RUN_STARTED", threadId: input.threadId, runId: input.runId })}\n\n`,
      { headers: { "content-type": "text/event-stream" } });
  });
  await assert.rejects(runAgentMessage("/api/v1/agent", "conversation", input, new AbortController().signal,
    () => {}, () => {}), /before completion/);
});
