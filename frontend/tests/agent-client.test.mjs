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
    ...(success ? [{ type: "CUSTOM", name: "shovel.message", value: { id: "saved-answer", content: "private-persisted-text", citations: [], warnings: ["private-warning"] } }] : []),
    success
      ? { type: "RUN_FINISHED", threadId: input.threadId, runId: input.runId }
      : { type: "RUN_ERROR", message: "Citation validation failed" },
  ];
  return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), {
    headers: { "content-type": "text/event-stream", "X-Shovel-Request-ID": "request-123" },
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

test("explicit Chat mode reaches the server without changing legacy requests", async (t) => {
  let captured;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    captured = JSON.parse(options.body);
    return response(captured);
  });
  await runAgentMessage("/api/v1/agent", "conversation",
    { content: "Hello", resource_ids: [], memory_enabled: true, search_mode: "auto", interaction_mode: "chat" },
    new AbortController().signal, () => {}, () => {});
  assert.deepEqual(captured.forwardedProps,
    { resource_ids: [], memory_enabled: true, search_mode: "auto", interaction_mode: "chat" });
});

test("AG-UI renders incremental text before the response body and durable run finish", { timeout: 5000 }, async (t) => {
  let controller;
  let closed = false;
  let firstText;
  const received = new Promise((resolve) => { firstText = resolve; });
  let finished = false;
  const encoder = new TextEncoder();
  const emit = (event) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    const request = JSON.parse(options.body);
    return new Response(new ReadableStream({
      start(stream) {
        controller = stream;
        emit({ type: "RUN_STARTED", threadId: request.threadId, runId: request.runId });
        emit({ type: "TEXT_MESSAGE_START", messageId: "answer", role: "assistant" });
        emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "answer", delta: "Early text" });
      },
    }), { headers: { "content-type": "text/event-stream", "cache-control": "no-cache, no-transform" } });
  });
  t.after(() => { if (!closed) controller?.close(); });
  const run = runAgentMessage("/api/v1/agent", "session", input, new AbortController().signal,
    (text) => { assert.equal(text, "Early text"); firstText(); }, () => {}, "incremental-run").then(() => { finished = true; });
  await received;
  assert.equal(finished, false);
  emit({ type: "TEXT_MESSAGE_END", messageId: "answer" });
  emit({ type: "RUN_FINISHED", threadId: "session", runId: "incremental-run" });
  closed = true;
  controller.close();
  await run;
  assert.equal(finished, true);
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

for (const environment of ["development", "production"]) {
  for (const outcome of ["success", "failure", "cancelled", "network"]) {
    test(`AG-UI ${environment} ${outcome} diagnostics correlate metadata without bodies`, async (t) => {
      const previous = process.env.NODE_ENV;
      process.env.NODE_ENV = environment;
      t.after(() => {
        if (previous === undefined) delete process.env.NODE_ENV;
        else process.env.NODE_ENV = previous;
      });
      const messages = [];
      t.mock.method(console, "info", (line) => messages.push(line));
      t.mock.method(console, "error", () => {});
      t.mock.method(globalThis, "fetch", async (_url, options) => {
        const request = JSON.parse(options.body);
        assert.equal(request.runId, "run-123");
        if (outcome === "network") throw new TypeError("private-network-details");
        return response(request, outcome !== "failure");
      });
      const controller = new AbortController();
      if (outcome === "cancelled") controller.abort();
      const promise = runAgentMessage("/api/v1/agent", "session-123",
        { ...input, content: "private-question" }, controller.signal, () => {}, () => {}, "run-123");
      if (outcome === "success") await promise;
      else await assert.rejects(promise);
      if (environment === "production") {
        assert.deepEqual(messages, []);
        return;
      }
      const entries = messages.map((line) => JSON.parse(line.slice("[shovel.chat] ".length)));
      const terminal = outcome === "success" ? "agent.completed" : outcome === "cancelled" ? "agent.cancelled" : "agent.failed";
      assert.equal(entries.at(-1).event, terminal);
      assert.ok(entries.every((entry) => entry.run_id === "run-123" && entry.conversation_id === "session-123" && entry.component === "frontend"));
      if (outcome === "success" || outcome === "failure") {
        assert.equal(entries.find((entry) => entry.event === "http.response").request_id, "request-123");
        assert.equal(entries.find((entry) => entry.event === "agent.first_text").elapsed_ms >= 0, true);
      }
      if (outcome === "success") {
        assert.equal(entries.find((entry) => entry.event === "agent.message.persisted").message_id, "saved-answer");
        assert.equal(entries.at(-1).output_chars, "Streamed answer".length);
        assert.equal(entries.at(-1).chunks, 1);
      } else {
        assert.ok(!entries.some((entry) => entry.event === "agent.completed"));
      }
      const output = messages.join("\n");
      for (const secret of ["private-", "Streamed answer", "Citation validation failed"]) {
        assert.ok(!output.includes(secret), output);
      }
    });
  }
}
