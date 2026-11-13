import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import * as format from "../app/message-format.ts";
import { SessionDraft } from "../app/session-drafts.ts";
import { translate } from "../app/i18n.ts";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";

class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

const status = { items: [], limits: { extensions: [".txt", ".png"], max_files: 5, max_file_bytes: 10 * 1024 * 1024 } };
function attachmentHook(api, draft = new SessionDraft(), policy = status) {
  const slots = [];
  const cleanups = [];
  const timers = [];
  let index = 0;
  const react = {
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
    useState(initial) {
      const slot = index++;
      if (!(slot in slots)) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], next => { slots[slot] = typeof next === "function" ? next(slots[slot]) : next; }];
    },
    useRef(initial) {
      const slot = index++;
      if (!(slot in slots)) slots[slot] = { current: initial };
      return slots[slot];
    },
    useEffect(effect) {
      const slot = index++;
      if (!(slot in slots)) { slots[slot] = true; cleanups.push(effect()); }
    },
  };
  const source = readFileSync(new URL("../app/chat-attachments.tsx", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const require = createRequire(import.meta.url);
  const exports = {};
  const dependencies = {
    react,
    "./api": { api, ApiError, errorText: error => error.message, segment: encodeURIComponent,
      uploadFile: (path, file, signal, onProgress) => api(path, { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file, signal, onProgress }) },
    "./message-format": format, "./locale-context": { useI18n: () => ({ t: (key, values) => translate(key, "en", values) }) },
  };
  new Function("require", "exports", "setTimeout", "clearTimeout", outputText)(
    id => dependencies[id] ?? require(id), exports, callback => { timers.push(callback); return 0; }, () => {});
  return {
    draft,
    timers,
    exports,
    render() { index = 0; return exports.useAttachments("session/a", draft, policy); },
    unmount() { cleanups.forEach(cleanup => cleanup?.()); },
  };
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const file = (name = "note.txt", size = 4) => new File(["x".repeat(size)], name);
const metadata = path => ({ id: path.split("?")[0].split("/").at(-1), name: "note.txt", size: 4, media_type: "text/plain", expires_at: 1800000000 });

test("Uploads are bounded before dispatch, use scoped binary requests, and block sending until complete", async () => {
  const calls = [];
  const releases = [];
  const hook = attachmentHook((path, options) => {
    calls.push({ path, options });
    return new Promise(resolve => releases.push(() => resolve(metadata(path))));
  });
  hook.render().add(Array.from({ length: 6 }, () => file()));
  assert.equal(calls.length, 5);
  assert.equal(hook.render().uploading, true);
  assert.equal(hook.render().invalid, true);
  assert.match(hook.render().error, /limit has been reached/);
  assert.ok(calls.every(call => call.path.startsWith("/conversations/session%2Fa/attachments/")));
  assert.ok(calls.every(call => call.options.method === "PUT" && call.options.body instanceof File && call.options.headers["Content-Type"] === "application/octet-stream"));
  releases.forEach(release => release());
  await settle();
  assert.equal(hook.render().completed.length, 5);
  assert.equal(hook.render().uploading, false);
  assert.equal(hook.render().invalid, false);
  hook.render().clear();
  assert.equal(hook.render().uploads.length, 0);
});

test("Rejected/lost uploads retry the same identity and are removable even if no server file exists", async () => {
  const calls = [];
  let succeed = false;
  const hook = attachmentHook(async (path, options) => {
    calls.push({ path, options });
    if (options.method === "DELETE") throw new ApiError("Missing", 404);
    if (!succeed) throw new ApiError("Upload outcome unknown", 502);
    return metadata(path);
  });
  hook.render().add([file("space & name.txt")]);
  await settle();
  const failed = hook.render().uploads[0];
  assert.match(failed.error, /outcome unknown/);
  assert.equal(hook.render().invalid, true);
  succeed = true;
  await hook.render().retry(failed.id, failed.file);
  assert.equal(calls[0].path, calls[1].path);
  assert.ok(calls[0].path.endsWith("?filename=space%20%26%20name.txt"));
  assert.equal(hook.render().invalid, false);
  await hook.render().remove(failed.id);
  assert.equal(hook.render().uploads.length, 0);
});

test("File bounds and deletion errors remain visible; switching views preserves uploads until Session deletion", async () => {
  const calls = [];
  const hook = attachmentHook(async (path, options) => {
    calls.push({ path, options });
    if (options.method === "DELETE") throw new ApiError("Deletion denied", 409);
    return new Promise(() => {});
  });
  hook.render().add([file("empty.txt", 0), file("large.txt", 10 * 1024 * 1024 + 1)]);
  assert.equal(calls.length, 0);
  assert.match(hook.render().error, /exceeds the server upload limit/);
  hook.render().add([file()]);
  const id = hook.render().uploads[0].id;
  await hook.render().remove(id);
  assert.equal(hook.render().uploads.length, 1);
  assert.equal(hook.render().error, "Deletion denied");
  hook.render().add([file("another.txt")]);
  const active = calls.at(-1);
  hook.unmount();
  assert.equal(active.options.signal.aborted, false);
  hook.draft.dispose();
  assert.equal(active.options.signal.aborted, true);
});

test("Removing a bound draft preserves history, while other conflicts remain visible", async () => {
  let bound = true;
  const hook = attachmentHook(async (path, options) => {
    if (options.method === "DELETE") throw new ApiError(bound
      ? "Attached files belong to their message; delete the Session instead." : "Another conflict", 409, bound ? "AttachmentBound" : "RuntimeFailure");
    return metadata(path);
  });
  hook.render().add([file()]);
  await settle();
  await hook.render().remove(hook.render().uploads[0].id);
  assert.equal(hook.render().uploads.length, 0);
  assert.match(hook.render().notice, /remains available in Session history/);
  bound = false;
  hook.render().add([file()]);
  await settle();
  await hook.render().remove(hook.render().uploads[0].id);
  assert.equal(hook.render().uploads.length, 1);
  assert.equal(hook.render().error, "Another conflict");
});

test("Returning to a Session recovers the same pending upload and its completed identity without reuploading", async () => {
  const calls = [];
  let release;
  const api = (path, options) => {
    calls.push({ path, options });
    return new Promise(resolve => { release = () => resolve(metadata(path)); });
  };
  const first = attachmentHook(api);
  first.render().add([file()]);
  const id = first.render().uploads[0].id;
  first.unmount();
  const returned = attachmentHook(api, first.draft);
  assert.equal(returned.render().uploads[0].id, id);
  assert.equal(returned.render().uploading, true);
  release();
  await settle();
  assert.equal(returned.render().completed[0].id, id);
  assert.equal(returned.render().uploading, false);
  assert.equal(calls.length, 1);
});

test("Restored expired attachments block sending and are never included in completed inputs", () => {
  const draft = new SessionDraft();
  draft.update({ uploads: [{ id: "expired", file: file(), uploading: false,
    attachment: { ...metadata("/expired"), expires_at: 1 } }] });
  const hook = attachmentHook(() => assert.fail("Restoring a draft must not reupload automatically"), draft);
  assert.equal(hook.render().invalid, true);
  assert.deepEqual(hook.render().completed, []);
  hook.timers[0]();
  assert.match(hook.render().uploads[0].error, /expired/);
});

test("Progress, cancellation and retry preserve UUID and fence stale callbacks without losing the file", async () => {
  const calls = [];
  const releases = [];
  const hook = attachmentHook((path, options) => {
    calls.push({ path, options });
    return new Promise(resolve => releases.push(() => resolve(metadata(path))));
  });
  hook.render().add([file()]);
  const first = calls[0];
  const id = hook.render().uploads[0].id;
  first.options.onProgress(0.45);
  assert.equal(hook.render().uploads[0].progress, 0.45);
  first.options.onProgress(1);
  assert.equal(hook.render().uploading, true, "Transfer completion is not server acknowledgement");
  hook.render().cancel(id);
  assert.equal(first.options.signal.aborted, true);
  assert.equal(hook.render().uploads[0].uploading, false);
  assert.match(hook.render().uploads[0].error, /cancelled/);
  assert.equal(hook.render().invalid, true);
  const retry = hook.render().retry(id, hook.render().uploads[0].file);
  assert.equal(calls[1].path, calls[0].path);
  first.options.onProgress(0.9);
  releases[0]();
  await settle();
  assert.equal(hook.render().uploads[0].progress, 0);
  assert.equal(hook.draft.requests.get(id).signal, calls[1].options.signal);
  calls[1].options.onProgress(1);
  releases[1]();
  await retry;
  assert.equal(hook.render().completed[0].id, id);
});

test("Server capabilities govern admission and unavailable files cannot be sent", async () => {
  const unavailable = attachmentHook(() => assert.fail("Cannot upload without server limits"), new SessionDraft(), null);
  unavailable.render().add([file()]);
  assert.match(unavailable.render().error, /limits are unavailable/);
  const policy = { ...status, items: [] };
  const hook = attachmentHook(async path => metadata(path), new SessionDraft(), policy);
  hook.render().add([file("bad.exe")]);
  assert.match(hook.render().error, /Unsupported/);
  hook.render().add([file()]);
  await settle();
  policy.items = [{ ...hook.render().completed[0], available: false }];
  assert.equal(hook.render().invalid, true);
  assert.deepEqual(hook.render().completed, []);
});

test("Reading scope distinguishes truncated excerpts, sanitized image dimensions and unknown legacy details", () => {
  const { AttachmentScope } = attachmentHook(() => {}).exports;
  const render = item => renderToStaticMarkup(createElement(AttachmentScope, { item }));
  assert.match(render({ reading_scope: { original_bytes: 9000, stored_bytes: 9000, excerpt_chars: 8000, truncated: true } }), /8000.*Truncated/);
  assert.match(render({ reading_scope: { original_width: 3000, original_height: 1200, width: 2048, height: 819 } }), /3000.*1200.*2048.*819/);
  assert.match(render({ reading_scope: {} }), /unavailable for this older attachment/);
});
