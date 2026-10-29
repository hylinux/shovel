import assert from "node:assert/strict";
import test from "node:test";
import { startQueryPolling } from "../app/query-polling.ts";

class Page extends EventTarget {
  hidden = false;
  show(visible) {
    this.hidden = !visible;
    this.dispatchEvent(new Event("visibilitychange"));
  }
}

function setup(t, hidden = false) {
  const page = new Page();
  page.hidden = hidden;
  const previous = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: page });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "document", previous);
    else delete globalThis.document;
  });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  return page;
}

async function settled() {
  await Promise.resolve();
  await Promise.resolve();
}

test("Periodic reads pause while hidden, refresh on return, and stop on cleanup", async (t) => {
  const page = setup(t);
  const signals = [];
  const stop = startQueryPolling(async (signal) => { signals.push(signal); }, 2000);
  t.after(stop);
  await settled();
  assert.equal(signals.length, 1);
  t.mock.timers.tick(2000);
  await settled();
  assert.equal(signals.length, 2);
  page.show(false);
  t.mock.timers.tick(20000);
  await settled();
  assert.equal(signals.length, 2);
  page.show(true);
  await settled();
  assert.equal(signals.length, 3);
  stop();
  page.show(false);
  page.show(true);
  t.mock.timers.tick(20000);
  await settled();
  assert.equal(signals.length, 3);
  assert.ok(signals.every((signal) => signal.aborted));
});

test("A periodic query mounted in a hidden tab waits for visibility", async (t) => {
  const page = setup(t, true);
  let calls = 0;
  const stop = startQueryPolling(async () => { calls++; }, 2000);
  t.after(stop);
  t.mock.timers.tick(20000);
  await settled();
  assert.equal(calls, 0);
  page.show(true);
  await settled();
  assert.equal(calls, 1);
});

test("Visibility changes never overlap requests or cancel an in-flight read", async (t) => {
  const page = setup(t);
  let release;
  let calls = 0;
  let signal;
  const stop = startQueryPolling(async (requestSignal) => {
    signal = requestSignal;
    calls++;
    await new Promise((resolve) => { release = resolve; });
  }, 2000);
  t.after(stop);
  page.show(false);
  assert.equal(signal.aborted, false);
  page.show(true);
  page.show(true);
  assert.equal(calls, 1);
  page.show(false);
  release();
  await settled();
  t.mock.timers.tick(20000);
  assert.equal(calls, 1);
  page.show(true);
  assert.equal(calls, 2);
  stop();
  release();
  await settled();
  page.show(true);
  t.mock.timers.tick(20000);
  assert.equal(calls, 2);
  assert.equal(signal.aborted, true);
});

test("One-off reads still run in a hidden tab and do not refresh on visibility changes", async (t) => {
  const page = setup(t, true);
  let calls = 0;
  const stop = startQueryPolling(async () => { calls++; }, 0);
  t.after(stop);
  await settled();
  assert.equal(calls, 1);
  page.show(true);
  page.show(false);
  t.mock.timers.tick(20000);
  await settled();
  assert.equal(calls, 1);
});
