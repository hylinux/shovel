import assert from "node:assert/strict";
import test from "node:test";
import { resolveInitialSession, sessionCacheKey } from "../app/session-startup.ts";

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

test("Page reloads reuse one workspace Session instead of creating more", async () => {
  const saved = storage();
  let creations = 0;
  const create = async () => `session-${++creations}`;
  const lookup = async (id) => assert.equal(id, "session-1");
  for (let reload = 0; reload < 4; reload++) {
    assert.equal(await resolveInitialSession("workspace", saved, lookup, create), "session-1");
  }
  assert.equal(creations, 1);
});

test("Only a deleted Session is replaced, and its replacement is reused", async () => {
  const saved = storage();
  saved.setItem(sessionCacheKey("workspace"), "deleted");
  let creations = 0;
  const lookup = async (id) => {
    if (id === "deleted") throw Object.assign(new Error("Not found"), { status: 404 });
    assert.equal(id, "replacement");
  };
  const create = async () => { creations++; return "replacement"; };
  assert.equal(await resolveInitialSession("workspace", saved, lookup, create), "replacement");
  assert.equal(await resolveInitialSession("workspace", saved, lookup, create), "replacement");
  assert.equal(creations, 1);
});

test("Network, permission and server errors do not create or forget Sessions", async () => {
  for (const status of [undefined, 403, 409, 500, 503]) {
    const saved = storage();
    saved.setItem(sessionCacheKey("workspace"), "existing");
    const error = Object.assign(new Error("Lookup failed"), { status });
    await assert.rejects(
      resolveInitialSession("workspace", saved, async () => { throw error; }, async () => {
        assert.fail("A failed lookup must not create another Session");
      }),
      (caught) => caught === error,
    );
    assert.equal(saved.getItem(sessionCacheKey("workspace")), "existing");
  }
});

test("Workspaces remain isolated and explicitly selected Sessions are restored", async () => {
  const saved = storage();
  const lookup = async () => {};
  assert.equal(await resolveInitialSession("first", saved, lookup, async () => "first-session"), "first-session");
  assert.equal(await resolveInitialSession("second", saved, lookup, async () => "second-session"), "second-session");
  saved.setItem(sessionCacheKey("first"), "manual-session");
  const noCreation = async () => assert.fail("An existing Session must be restored");
  assert.equal(await resolveInitialSession("first", saved, lookup, noCreation), "manual-session");
  assert.equal(await resolveInitialSession("second", saved, lookup, noCreation), "second-session");
});
