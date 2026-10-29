import assert from "node:assert/strict";
import test from "node:test";
import { mergeSessionHistory } from "../app/session-history.ts";

test("Session history follows latest interaction rather than creation time", () => {
  const older = { id: "older", title: "Older session", created_at: 1, updated_at: 10 };
  const newer = { id: "newer", title: "Newer session", created_at: 5, updated_at: 20 };
  const result = mergeSessionHistory([newer, older], [{ ...older, updated_at: 30 }]);
  assert.deepEqual(result.map((session) => session.id), ["older", "newer"]);
  assert.equal(older.updated_at, 10);
});

test("Overlapping history pages do not duplicate or overwrite newer session metadata", () => {
  const current = { id: "current", title: "Renamed", created_at: 1, updated_at: 30 };
  const result = mergeSessionHistory([current], [
    { ...current, title: "Outdated", updated_at: 20 },
    { id: "older", title: "Older", created_at: 0, updated_at: 10 },
  ]);
  assert.deepEqual(result.map(({ id, title }) => ({ id, title })), [
    { id: "current", title: "Renamed" },
    { id: "older", title: "Older" },
  ]);
});
