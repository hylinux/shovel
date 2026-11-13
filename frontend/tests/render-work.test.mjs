import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { batchStreamText } from "../app/stream-batch.ts";
import { attachmentNow, subscribeAttachmentClock } from "../app/attachment-clock.ts";
import * as format from "../app/message-format.ts";

test("A stream burst coalesces latest full text, flushes completion and cancels abandoned work", () => {
  const callbacks = new Map();
  const values = [];
  let next = 0;
  const batch = batchStreamText(value => values.push(value), callback => { callbacks.set(++next, callback); return next; }, id => callbacks.delete(id));
  for (let index = 0; index < 240; index++) batch.push(`full text ${index}`);
  assert.equal(callbacks.size, 1);
  assert.deepEqual(values, []);
  callbacks.values().next().value();
  assert.deepEqual(values, ["full text 239"]);
  batch.push("last characters");
  batch.flush();
  assert.equal(callbacks.size, 0);
  assert.deepEqual(values, ["full text 239", "last characters"]);
  batch.push("cancelled");
  batch.cancel();
  assert.equal(callbacks.size, 0);
  assert.equal(values.at(-1), "last characters");
});

test("Attachment cards share a single clock and release it after the last subscriber", () => {
  const originalCreate = globalThis.setInterval;
  const originalRemove = globalThis.clearInterval;
  const timers = new Map();
  let ticks = 0;
  globalThis.setInterval = callback => { timers.set(1, callback); return 1; };
  globalThis.clearInterval = id => timers.delete(id);
  try {
    const subscriptions = Array.from({ length: 100 }, () => subscribeAttachmentClock(() => { ticks++; }));
    assert.equal(timers.size, 1);
    assert.ok(attachmentNow() > 0);
    timers.get(1)();
    assert.equal(ticks, 100);
    subscriptions.slice(0, -1).forEach(remove => remove());
    assert.equal(timers.size, 1);
    subscriptions.at(-1)();
    assert.equal(timers.size, 0);
  } finally { globalThis.setInterval = originalCreate; globalThis.clearInterval = originalRemove; }
});

function queueType() {
  const require = createRequire(import.meta.url);
  const source = readFileSync(new URL("../app/preview-renderer.ts", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
  const exports = {};
  new Function("require", "exports", outputText)(id => id === "./message-format" ? format : require(id), exports);
  return exports.PreviewQueue;
}

test("Preview work is serial, identical sources reuse sanitized results, and errors/cancellation do not poison later work", async () => {
  const PreviewQueue = queueType();
  const calls = [];
  let active = 0;
  let maximum = 0;
  const queue = new PreviewQueue(async source => {
    calls.push(source); active++; maximum = Math.max(maximum, active);
    await Promise.resolve();
    active--;
    if (source === "bad") throw new Error("Invalid diagram");
    return `sanitized ${source}`;
  });
  const signal = () => new AbortController().signal;
  assert.deepEqual(await Promise.all([queue.run("same", "mermaid", signal()), queue.run("same", "mermaid", signal()), queue.run("other", "mermaid", signal())]),
    ["sanitized same", "sanitized same", "sanitized other"]);
  assert.equal(maximum, 1);
  assert.deepEqual(calls, ["same", "other"]);
  await assert.rejects(queue.run("bad", "mermaid", signal()), /Invalid diagram/);
  const cancelled = new AbortController();
  const aborted = queue.run("cancelled", "mermaid", cancelled.signal);
  cancelled.abort();
  await assert.rejects(aborted, { name: "AbortError" });
  assert.equal(await queue.run("after", "mermaid", signal()), "sanitized after");
  assert.ok(!calls.includes("cancelled"));
  for (let index = 0; index < 17; index++) await queue.run(`new ${index}`, "mermaid", signal());
  await queue.run("same", "mermaid", signal());
  assert.equal(calls.filter(source => source === "same").length, 2, "Old previews must be evicted");
});
