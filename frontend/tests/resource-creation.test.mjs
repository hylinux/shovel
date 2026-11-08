import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { translate } from "../app/i18n.ts";

const require = createRequire(import.meta.url);
const sources = Object.fromEntries(["resources", "ui"].map(file => [file, ts.transpileModule(
  readFileSync(new URL(`../app/${file}.tsx`, import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } },
).outputText]));
const queued = {
  id: "creation/task", kind: "resource.create", state: "queued", resource_id: null,
  job_id: null, result: {}, error: null, queued_at: 1700000000,
};

function renderNotice({ data, error = "", locale = "en" } = {}) {
  let query;
  const loaded = {};
  function load(file) {
    if (loaded[file]) return loaded[file];
    const exports = {};
    new Function("require", "exports", sources[file])(id => {
      if (id === "./api") return {
        active: state => ["queued", "running"].includes(state),
        segment: encodeURIComponent,
        useQuery: (path, interval, shouldPoll) => {
          query = { path, interval, shouldPoll };
          return { data, error, refresh: () => {} };
        },
      };
      if (id === "./locale-context") return { useI18n: () => ({ t: (message, values) => translate(message, locale, values) }) };
      if (id === "./ui") return load("ui");
      if (id === "./profiles" || id === "./pagination-query") return {};
      return require(id);
    }, exports);
    loaded[file] = exports;
    return exports;
  }
  const { ResourceCreationNotice } = load("resources");
  const html = renderToStaticMarkup(createElement(ResourceCreationNotice, {
    operation: queued, name: "User supplied <folder>", onFinished: () => {}, onTasks: () => {}, onDismiss: () => {},
  }));
  return { html, query };
}

test("An acknowledged queued creation remains visible while its receipt loads", () => {
  const { html, query } = renderNotice();
  assert.match(html, /Resource creation queued.*running scans finish/);
  assert.match(html, /role="status"/);
  assert.match(html, /User supplied &lt;folder&gt;/);
  assert.match(html, /Creation task: creation\/task/);
  assert.match(html, /View in Background tasks/);
  assert.doesNotMatch(html, /feedback success|Dismiss notification/);
  assert.equal(query.path, "/operations/creation%2Ftask");
  assert.equal(query.interval, 2000);
  assert.equal(query.shouldPoll(queued), true);
});

test("Receipt updates distinguish running from completed creation and stop terminal polling", () => {
  const running = renderNotice({ data: { ...queued, state: "running" } });
  assert.match(running.html, /Creating and verifying the resource.*not be listed yet/);
  assert.doesNotMatch(running.html, /feedback success|Dismiss notification/);
  assert.equal(running.query.shouldPoll({ ...queued, state: "running" }), true);

  const completed = renderNotice({ data: { ...queued, state: "succeeded" } });
  assert.match(completed.html, /Resource creation completed.*filter and pagination/);
  assert.match(completed.html, /feedback success/);
  assert.match(completed.html, /Dismiss notification/);
  assert.equal(completed.query.shouldPoll({ ...queued, state: "succeeded" }), false);
});

test("Creation and verification failures expose details without suggesting automatic resubmission", () => {
  for (const state of ["failed", "completed_with_errors", "cancelled"]) {
    const detail = state === "completed_with_errors" ? { result: { message: "Verification denied <details>" } }
      : { error: "Creation denied <details>" };
    const { html, query } = renderNotice({ data: { ...queued, ...detail, state } });
    assert.match(html, /role="alert"/);
    assert.match(html, /denied &lt;details&gt;/);
    assert.match(html, /Check Background tasks/);
    assert.doesNotMatch(html, /feedback success/);
    assert.equal(query.shouldPoll({ ...queued, state }), false);
  }
});

test("Receipt read failures retain the accepted task identity and offer read retry", () => {
  const { html } = renderNotice({ error: "Cannot read creation receipt" });
  assert.match(html, /Resource creation queued/);
  assert.match(html, /creation\/task/);
  assert.match(html, /Cannot read creation receipt/);
  assert.match(html, /<button>Retry<\/button>/);
  assert.doesNotMatch(html, /feedback success/);
});

test("Creation feedback is localized without translating resource names or task IDs", () => {
  for (const state of ["queued", "running", "succeeded", "completed_with_errors", "failed", "cancelled", "unknown"]) {
    const { html } = renderNotice({ data: { ...queued, state }, locale: "zh-CN" });
    assert.doesNotMatch(html, /Resource creation|Creating and verifying|View in Background tasks/);
    assert.match(html, /User supplied &lt;folder&gt;/);
    assert.match(html, /creation\/task/);
    if (state === "unknown") assert.doesNotMatch(html, /feedback success/);
  }
});
