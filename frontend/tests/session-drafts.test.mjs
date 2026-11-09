import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { SessionDrafts } from "../app/session-drafts.ts";
import * as format from "../app/message-format.ts";
import * as memory from "../app/memory-model.ts";

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...elements(tree.props?.children)];
}

function composer(run = async () => false) {
  const drafts = new SessionDrafts();
  const source = readFileSync(new URL("../app/knowledge.tsx", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const react = {
    useState: initial => [typeof initial === "function" ? initial() : initial, () => {}],
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
    useRef: () => ({ current: null }), useEffect() {},
  };
  function ComposerTools() { return null; }
  const dependencies = {
    react,
    "./locale-context": { useI18n: () => ({ t: key => key }) },
    "./api": { segment: encodeURIComponent, useAgentAction: () => ({ busy: false, run }),
      useQuery: () => ({ data: { items: [] }, loading: false, error: "", refresh() {} }) },
    "./ui": { Empty: () => null, Feedback: () => null, Sources: () => null },
    "./agent-client": { chatTrace() {} }, "./memory-model": memory,
    "./working-memory": { WorkingMemoryPanel: () => null },
    "./message-content": { MessageContent: () => null, RichText: () => null },
    "./message-format": format, "./composer-tools": { ComposerTools },
    "./chat-attachments": { MessageAttachments: () => null, UploadList: () => null,
      useAttachments: () => ({ uploads: [], completed: [], uploading: false, invalid: false }) },
  };
  const exports = {};
  const require = createRequire(import.meta.url);
  new Function("require", "exports", outputText)(id => id === "./icon.svg"
    ? { default: "/icon.svg" } : dependencies[id] ?? require(id), exports);
  return {
    drafts,
    render(session, workspace = "workspace") {
      const root = exports.Knowledge({ sessionId: session, status: { workspace_id: workspace, model_configured: true, memory_configured: true },
        drafts, onSessionChanged() {} });
      const chat = elements(root).find(node => typeof node.type === "function" && node.type.name === "Chat");
      return elements(chat.type(chat.props));
    },
    input(nodes) { return nodes.find(node => node.type === "textarea"); },
    tools(nodes) { return nodes.find(node => node.type === ComposerTools); },
    send(nodes) { return nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} }); },
  };
}

test("Session remount restores composer text, format and preview without leaking between Sessions or workspaces", () => {
  const ui = composer();
  ui.input(ui.render("a")).props.onChange({ target: { value: '{"a":1}' } });
  ui.tools(ui.render("a")).props.onFormat("json");
  ui.tools(ui.render("a")).props.onPreview();
  ui.input(ui.render("b")).props.onChange({ target: { value: "B draft" } });
  const returned = ui.render("a");
  assert.equal(ui.input(returned).props.value, '{"a":1}');
  assert.equal(ui.input(returned).props.hidden, true);
  assert.equal(ui.tools(returned).props.format, "json");
  assert.equal(ui.input(ui.render("b")).props.value, "B draft");
  assert.equal(ui.input(ui.render("a", "another-workspace")).props.value, "");
});

test("A settled send clears only its unchanged Session draft, never another or a newer draft", async () => {
  const releases = [];
  const ui = composer(() => new Promise(resolve => releases.push(resolve)));
  ui.input(ui.render("a")).props.onChange({ target: { value: "A question" } });
  const sending = ui.send(ui.render("a"));
  ui.input(ui.render("b")).props.onChange({ target: { value: "B draft" } });
  releases.shift()(true);
  await sending;
  assert.equal(ui.input(ui.render("a")).props.value, "");
  assert.equal(ui.input(ui.render("b")).props.value, "B draft");
  ui.input(ui.render("a")).props.onChange({ target: { value: "A next question" } });
  const next = ui.send(ui.render("a"));
  ui.input(ui.render("a")).props.onChange({ target: { value: "Newer A draft" } });
  ui.input(ui.render("a")).props.onChange({ target: { value: "A next question" } });
  releases.shift()(true);
  await next;
  assert.equal(ui.input(ui.render("a")).props.value, "A next question");
});

test("Failed sends retain text and format; Session deletion aborts uploads and prevents late state resurrection", async () => {
  const ui = composer();
  ui.input(ui.render("a")).props.onChange({ target: { value: "Uncertain question" } });
  ui.tools(ui.render("a")).props.onFormat("html");
  await ui.send(ui.render("a"));
  assert.equal(ui.input(ui.render("a")).props.value, "Uncertain question");
  assert.equal(ui.tools(ui.render("a")).props.format, "html");
  const removed = ui.drafts.get("workspace", "a");
  const request = new AbortController();
  removed.requests.set("upload", request);
  ui.drafts.delete("workspace", "a");
  removed.update({ content: "Late callback" });
  assert.equal(request.signal.aborted, true);
  assert.equal(ui.input(ui.render("a")).props.value, "");
});
