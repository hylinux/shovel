import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { isPage, pagePath, pagination } from "../app/pagination.ts";
import { orderSearchResults } from "../app/search-results.ts";
import { translate } from "../app/i18n.ts";

const require = createRequire(import.meta.url);
function load(name, overrides) {
  const source = readFileSync(new URL(`../app/${name}`, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  new Function("require", "exports", outputText)((id) => {
    if (id in overrides) return overrides[id];
    if (id.startsWith(".")) throw new Error(`Unexpected dependency: ${id}`);
    return require(id);
  }, exports);
  return exports;
}

test("page ranges cover partial last pages and clamp after items are removed", () => {
  const last = pagination(23, 3, 10);
  assert.deepEqual([last.start, last.end, last.offset, last.pageCount], [21, 23, 20, 3]);
  const removed = pagination(7, 3, 10);
  assert.deepEqual([removed.page, removed.start, removed.end], [1, 1, 7]);
  assert.deepEqual([pagination(0, 3, 10).start, pagination(0, 3, 10).end], [0, 0]);
});

test("search pagination slices globally sorted results without mutating retrieval order", () => {
  const items = Array.from({ length: 23 }, (_, index) => ({ title: `Title ${index}`, score: index }));
  const sorted = orderSearchResults(items, "score_desc");
  const page = pagination(items.length, 2, 10);
  assert.deepEqual(sorted.slice(page.offset, page.end).map((item) => item.score), [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]);
  assert.equal(items[0].score, 0);
});

function queryHarness() {
  let state;
  let changed;
  let snapshot = {};
  let requested;
  const invoke = load("pagination-query.ts", {
    react: { useState(initial) {
      state ??= initial;
      return [state, (next) => { state = typeof next === "function" ? next(state) : next; changed = true; }];
    } },
    "./api": { useQuery(path) { requested = path; return { ...snapshot, refresh() {} }; } },
    "./pagination": { isPage, pagePath, pagination },
  }).usePageQuery;
  return {
    response(path, total, items = []) { snapshot = { dataPath: path, data: { items, total } }; },
    failure(path) { snapshot = { errorPath: path, error: "Request failed" }; },
    invalid(path) { snapshot = { dataPath: path, data: { items: [] } }; },
    render(path = "/resources") {
      let result;
      for (let attempts = 0; attempts < 5; attempts++) {
        changed = false;
        result = invoke(path, 2000);
        if (!changed) return result;
      }
      throw new Error("Pagination did not settle.");
    },
    get requested() { return requested; },
  };
}

test("page navigation hides stale responses and errors from the previous page", () => {
  const harness = queryHarness();
  harness.response("/resources?limit=10&offset=0", 105, [{ id: "first" }]);
  let page = harness.render();
  page.setPage(11);
  page = harness.render();
  assert.equal(harness.requested, "/resources?limit=10&offset=100");
  assert.equal(page.loading, true);
  assert.equal(page.data, undefined);
  harness.failure("/resources?limit=10&offset=0");
  assert.equal(harness.render().error, "");
  harness.response(harness.requested, 105, [{ id: "last" }]);
  page = harness.render();
  assert.equal(page.page, 11);
  assert.equal(page.data.items[0].id, "last");
});

test("polling retains the page, shrinking totals clamp it, and filters or sizes reset it", () => {
  const harness = queryHarness();
  harness.response("/resources?limit=10&offset=0", 30);
  harness.render().setPage(3);
  harness.response("/resources?limit=10&offset=20", 31);
  assert.equal(harness.render().page, 3);
  harness.response("/resources?limit=10&offset=20", 15);
  let page = harness.render();
  assert.equal(page.page, 2);
  assert.equal(page.loading, true);
  assert.equal(harness.requested, "/resources?limit=10&offset=10");
  harness.response(harness.requested, 15);
  harness.render().setPageSize(20);
  page = harness.render();
  assert.equal(page.page, 1);
  assert.equal(harness.requested, "/resources?limit=20&offset=0");
  harness.response(harness.requested, 100);
  harness.render().setPage(3);
  page = harness.render("/resources?q=notes");
  assert.equal(page.page, 1);
  assert.equal(page.pageSize, 20);
  assert.equal(harness.requested, "/resources?q=notes&limit=20&offset=0");
  harness.invalid(harness.requested);
  assert.match(harness.render("/resources?q=notes").error, /invalid pagination metadata/);
});

for (const locale of ["en", "zh-CN"]) test(`all resource management lists render paged items and accessible count controls (${locale})`, () => {
  const react = { ...require("react"), useState(initial) { return [initial, () => {}]; } };
  const api = {
    date: () => "Recently", segment: encodeURIComponent, active: () => true,
    useQuery: () => ({ data: { items: [] }, loading: false, refresh() {} }),
    useAction: () => ({ busy: false }),
  };
  const localization = { useI18n: () => ({ t: (message, values) => translate(message, locale, values), date: () => "Recently" }) };
  const ui = load("ui.tsx", { "./api": api, "./locale-context": localization });
  const resourceItems = Array.from({ length: 10 }, (_, index) => ({
    id: `resource-${index}`, name: `Resource ${index}`, state: "active", connector_kind: "local_fs", config: {},
  }));
  const paged = (items) => ({ ...pagination(105, 2, 10), data: { items, total: 105 }, refresh() {}, setPage() {}, setPageSize() {} });
  const { Resources } = load("resources.tsx", {
    react, "./api": api, "./ui": ui, "./profiles": {}, "./locale-context": localization,
    "./pagination-query": { usePageQuery: () => paged(resourceItems) },
  });
  const resources = renderToStaticMarkup(createElement(Resources, { refresh() {} }));
  assert.equal((resources.match(/class="resource-row"/g) || []).length, 10);
  assert.match(resources, locale === "en" ? /11–20 of 105 resources/ : /11–20 \/ 105 资源/);
  assert.match(resources, locale === "en" ? /aria-label="Resources pagination"/ : /aria-label="资源分页"/);

  const jobs = resourceItems.map((item) => ({ ...item, scan_mode: "incremental", resource_id: item.id, state: "queued" }));
  const operations = resourceItems.map((item) => ({ ...item, kind: "resource.verify", state: "queued", result: {} }));
  const { Tasks } = load("tasks.tsx", {
    react, "./api": api, "./ui": ui, "./request-recovery": {}, "./locale-context": localization,
    "./pagination-query": { usePageQuery: (path) => paged(path === "/jobs" ? jobs : operations) },
  });
  const tasks = renderToStaticMarkup(createElement(Tasks, { resources: resourceItems }));
  assert.equal((tasks.match(/class="job-row"/g) || []).length, 10);
  assert.equal((tasks.match(/class="operation-row"/g) || []).length, 10);
  assert.match(tasks, locale === "en" ? /aria-label="Scan jobs pagination"/ : /aria-label="扫描作业分页"/);
  assert.match(tasks, locale === "en" ? /aria-label="Operations pagination"/ : /aria-label="操作分页"/);

  const items = resourceItems.map((item, index) => ({ ...item, chunk_id: item.id, score: index, title: item.name }));
  let slot = 0;
  const states = [[], "auto", "", "30", "score_desc", { mode: "keyword", items: [...items, ...items, ...items] }, 2, 10];
  const { SearchDocuments } = load("search.tsx", {
    react: { ...react, useState(initial) { return [states[slot++] ?? initial, () => {}]; } },
    "./api": api, "./ui": ui, "./pagination": { pagination }, "./search-results": { orderSearchResults }, "./locale-context": localization,
  });
  const search = renderToStaticMarkup(createElement(SearchDocuments, { resources: [] }));
  assert.equal((search.match(/class="source-number"/g) || []).length, 10);
  assert.match(search, /\[11\]/);
  assert.match(search, locale === "en" ? /11–20 of 30 search results/ : /11–20 \/ 30 搜索结果/);
});
