import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";

function transport() {
  const requests = [];
  let invalidations = 0;
  class Request {
    upload = {};
    headers = {};
    constructor() { requests.push(this); }
    open(method, url) { Object.assign(this, { method, url }); }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(body) { this.body = body; }
    abort() { this.onabort(); }
    complete(status, body) { this.status = status; this.responseText = body; this.onload(); }
  }
  const require = createRequire(import.meta.url);
  const source = readFileSync(new URL("../app/api.ts", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } });
  const exports = {};
  const dependencies = {
    "./read-requests": { invalidateReads: () => { invalidations++; } },
    "./agent-client": {}, "./request-recovery": {}, "./session-history": {}, "./query-polling": {}, "./platform-types": {},
  };
  new Function("require", "exports", "XMLHttpRequest", outputText)(
    id => dependencies[id] ?? require(id), exports, Request);
  return { ...exports, requests, invalidations: () => invalidations };
}

test("Binary upload exposes real transfer progress but resolves only after server validation", async () => {
  const api = transport();
  const progress = [];
  const file = new File(["synthetic"], "note.txt");
  const result = api.uploadFile("/conversations/session/attachments/id?filename=note.txt", file,
    new AbortController().signal, value => progress.push(value));
  const request = api.requests[0];
  assert.equal(request.url, "/api/v1/conversations/session/attachments/id?filename=note.txt");
  assert.equal(request.method, "PUT");
  assert.equal(request.body, file);
  assert.equal(request.headers["Content-Type"], "application/octet-stream");
  request.upload.onprogress({ lengthComputable: true, loaded: 4, total: 10 });
  request.upload.onload();
  assert.deepEqual(progress, [0.4, 1]);
  let settled = false;
  void result.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  request.complete(200, '{"id":"id"}');
  assert.deepEqual(await result, { id: "id" });
  assert.equal(api.invalidations(), 2);
});

test("Cancellation, timeout, network failures, invalid responses and server errors never look successful", async () => {
  for (const kind of ["abort", "timeout", "network", "invalid", "empty", "quota"]) {
    const api = transport();
    const controller = new AbortController();
    const progress = [];
    const result = api.uploadFile("/upload", new File(["data"], "note.txt"), controller.signal, value => progress.push(value));
    const request = api.requests[0];
    if (kind === "abort") controller.abort();
    if (kind === "timeout") request.ontimeout();
    if (kind === "network") request.onerror();
    if (kind === "invalid") request.complete(200, "not json");
    if (kind === "empty") request.complete(204, "");
    if (kind === "quota") request.complete(413, '{"error":{"code":"RuntimeFailure","message":"Quota exceeded"}}');
    await assert.rejects(result, error => {
      if (kind === "abort") return error.name === "AbortError";
      if (kind === "timeout") return /timed out/.test(error.message);
      if (kind === "network") return /Cannot reach/.test(error.message);
      if (kind === "invalid" || kind === "empty") return /invalid response/.test(error.message);
      return error.status === 413 && error.code === "RuntimeFailure" && error.message === "Quota exceeded";
    });
    if (kind === "abort") {
      request.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 });
      assert.deepEqual(progress, []);
    }
    assert.equal(api.invalidations(), 2);
  }
});
