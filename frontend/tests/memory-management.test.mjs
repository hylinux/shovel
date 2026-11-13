import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import ts from "typescript";
import * as model from "../app/memory-model.ts";
import { translate, localizedDate } from "../app/i18n.ts";
import * as messageFormat from "../app/message-format.ts";
import { SessionDrafts } from "../app/session-drafts.ts";

const messageComponents = {
  "./send-budget": { useSendBudget: () => ({ blocked: false }), SendBudgetNotice: () => null },
  "./session-scroll": { useSessionScroll: () => ({ messages: { current: null }, content: { current: null }, paused: false, unread: false, navigate() {}, bottom() {} }) },
  "./user-profile": { useUserProfile: () => ({ error: "", refresh() {} }), UserAvatar: () => createElement("svg") },
  "./message-content": { MessageContent: () => null, RichText: () => null },
  "./composer-tools": { ComposerTools: () => null },
  "./message-format": messageFormat,
  "./chat-attachments": {
    MessageAttachments: () => null, UploadList: () => null,
    useAttachments: () => ({ uploads: [], completed: [], uploading: false, invalid: false, error: "", clear() {} }),
  },
};

const fact = { id: "fact", memory: "我偏好中文", kind: "semantic", scope: "session", session_id: "session/a",
  state: "active", revision: 2, confidence: 0.78, source: "inferred", subject: "user", predicate: "prefers", object: "中文",
  provenance: [{ evidence: "请用中文回复", source_kind: "stated", conversation_id: "session/a", message_id: "message-1", asserted_at: 1700000000 }] };

test("Memory replacement preserves kind, scope and source Session without translating payload content", () => {
  const draft = { ...model.memoryInput(fact), text: "  请始终用中文  ", kind: "procedural", scope: "agent", session_id: "other" };
  assert.deepEqual(model.memoryPayload(draft, fact), {
    ...draft, text: "请始终用中文", kind: "semantic", scope: "session", session_id: "session/a",
  });
  assert.equal(model.memoryPayload({ ...model.memoryInput(), text: "Fact" }).session_id, "");
  assert.throws(() => model.memoryPayload({ ...model.memoryInput(), text: "fact", scope: "session" }), /Choose a Session/);
  for (const source of ["recorded", "legacy_unspecified"]) {
    assert.equal(model.memoryInput({ ...fact, kind: "episodic", occurred_at: 1800000000, episode_time_source: source }).occurred_at, null);
  }
  assert.equal(model.memoryInput({ ...fact, kind: "episodic", occurred_at: 1800000000, episode_time_source: "explicit" }).occurred_at, 1800000000);
});

test("Working notes retain multiline editing but send bounded lists and the observed CAS revision", () => {
  const task = { ...model.newWorkingTask("task/a"), revision: 7, goal: "Review notes",
    constraints: model.lines("No external writes\n\n"), next_step: "用户决定下一步", evidence_message_ids: ["user-message"] };
  assert.equal(task.constraints.join("\n"), "No external writes\n\n");
  const payload = model.workingPayload(task);
  assert.equal(payload.expected_revision, 7);
  assert.deepEqual(payload.constraints, ["No external writes"]);
  assert.equal(payload.next_step, "用户决定下一步");
  assert.equal("task_id" in payload, false);
  assert.throws(() => model.workingPayload({ ...task, blockers: Array(21).fill("blocked") }), /at most 20/);
  assert.throws(() => model.workingPayload({ ...task, plan: ["x".repeat(501)] }), /500 characters/);
  assert.equal(model.selectedWorkingTask([task], task.task_id), task.task_id);
  assert.equal(model.selectedWorkingTask([{ ...task, state: "completed" }], task.task_id), null);
  assert.equal(model.selectedWorkingTask([{ ...task, state: "archived" }], task.task_id), null);
});

test("Non-semantic edits supply valid neutral wire defaults while new Memory stays agent-scoped", () => {
  for (const kind of ["episodic", "procedural"]) {
    const original = { ...fact, kind, subject: "", predicate: "", object: "", topic: "" };
    const draft = { ...model.memoryInput(original), text: "Updated user notes" };
    const payload = model.memoryPayload(draft, original);
    assert.equal(payload.subject, "user");
    assert.equal(payload.predicate, "knows_about");
    assert.equal(payload.text, "Updated user notes");
    assert.equal(payload.kind, kind);
    assert.equal(payload.scope, "session");
    assert.equal(payload.session_id, "session/a");
  }
  assert.equal(model.memoryPayload({ ...model.memoryInput(), text: "A new fact" }).scope, "agent");
});

test("Unrelated text edits retain explicit day precision, while changed timestamps use the instant default", () => {
  const original = { ...fact, kind: "episodic", occurred_at: Date.UTC(2026, 10, 8) / 1000,
    episode_time_source: "explicit", episode_time_precision: "day" };
  const draft = { ...model.memoryInput(original), text: "Corrected event description" };
  const payload = model.memoryPayload(draft, original);
  assert.equal(payload.occurred_at, original.occurred_at);
  assert.equal(payload.time_precision, "day");
  assert.equal(payload.text, "Corrected event description");
  const changed = model.memoryPayload({ ...draft, occurred_at: original.occurred_at + 60 }, original);
  assert.equal("time_precision" in changed, false);
  assert.equal(changed.occurred_at, original.occurred_at + 60);
  assert.equal("time_precision" in model.memoryPayload({ ...model.memoryInput(), text: "Existing default payload" }), false);
});

test("Working write and deletion use encoded task identity and CAS; lost/conflicting responses reload without replay", async () => {
  const task = { ...model.newWorkingTask("task/a?"), revision: 4, goal: "Review" };
  for (const remove of [false, true]) {
    const calls = [];
    const outcome = await model.writeWorkingTask(async (path, options) => {
      calls.push({ path, options });
      if (options) throw new Error("Outcome unknown or revision conflict");
      return { items: [{ ...task, revision: 5 }] };
    }, "/conversations/session/working-memory", task, remove);
    assert.equal(outcome.ok, false);
    assert.equal(outcome.items[0].revision, 5);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].path, `/conversations/session/working-memory/task%2Fa%3F${remove ? "?revision=4" : ""}`);
    assert.equal(calls[0].options.method, remove ? "DELETE" : "PUT");
    if (!remove) assert.equal(JSON.parse(calls[0].options.body).expected_revision, 4);
    assert.equal(calls[1].options, undefined);
  }
});

test("Failed working reconciliation preserves both write and read failures without claiming a recheck", async () => {
  const task = { ...model.newWorkingTask("task"), goal: "User draft" };
  for (const primary of [new Error("Outcome unknown"), Object.assign(new Error("Revision conflict"), { status: 409 })]) {
    const secondary = new Error("Snapshot read failed");
    const calls = [];
    const outcome = await model.writeWorkingTask(async (path, options) => {
      calls.push({ path, options });
      throw options ? primary : secondary;
    }, "/working-memory", task);
    assert.equal(outcome.ok, false);
    assert.equal(outcome.error, primary);
    assert.equal(outcome.reconciliationError, secondary);
    assert.equal(outcome.rechecked, false);
    assert.equal(outcome.needsReload, true);
    assert.equal(outcome.items, undefined);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].options.method, "PUT");
    assert.equal(calls[1].options, undefined);
    assert.equal(task.goal, "User draft");
  }
});

test("Rejected working input remains editable without replaying or discarding its draft", async () => {
  const task = { ...model.newWorkingTask("task"), goal: "User draft" };
  let calls = 0;
  const error = Object.assign(new Error("Invalid evidence"), { status: 422 });
  const outcome = await model.writeWorkingTask(async () => { calls++; throw error; }, "/working-memory", task);
  assert.equal(outcome.ok, false);
  assert.equal(outcome.needsReload, false);
  assert.equal(calls, 1);
  assert.equal(task.goal, "User draft");
});

function loadComponent(file, dependencies) {
  const require = createRequire(import.meta.url);
  const source = readFileSync(new URL(`../app/${file}`, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  new Function("require", "exports", outputText)(id => {
    if (file === "knowledge.tsx" && id === "react") return { ...require("react"), ...dependencies.react, memo: component => component };
    return id === "./icon.svg" ? { default: "/icon.svg" } : dependencies[id] ?? require(id);
  }, exports);
  return exports;
}

function locale(language) {
  return { t: (key, values) => translate(key, language, values), date: (value, precision) => localizedDate(value, language, precision) };
}

test("Evidence UI exposes extraction scores, exact source evidence and episode fallback in English and Chinese", () => {
  for (const language of ["en", "zh-CN"]) {
    const { MemoryEvidence } = loadComponent("memories.tsx", {
      "./api": {}, "./memory-model": model, "./locale-context": { useI18n: () => locale(language) }, "./ui": {},
    });
    const html = renderToStaticMarkup(MemoryEvidence({ item: { ...fact, kind: "episodic", episode_time_source: "recorded", occurred_at: 1700000000 } }));
    assert.ok(html.includes(translate("Extraction score (not verified truth)", language)));
    assert.ok(html.includes(translate("Recorded time fallback (not an asserted event time)", language)));
    assert.ok(html.includes("0.78"));
    assert.ok(html.includes("请用中文回复"));
    assert.ok(html.includes("session/a"));
    assert.ok(html.includes("message-1"));
    assert.ok(!html.includes("verified badge"));
    for (const source of ["recorded", "legacy_unspecified", "explicit"]) {
      const time = 1800000000;
      const dated = renderToStaticMarkup(MemoryEvidence({ item: { ...fact, kind: "episodic", episode_time_source: source, occurred_at: time } }));
      const occurred = dated.split(`<dt>${translate("Occurred", language)}</dt><dd>`)[1].split("</dd>")[0];
      assert.equal(occurred.includes(localizedDate(time, language)), source === "explicit");
      if (source === "legacy_unspecified") assert.ok(occurred.includes(translate("Legacy episode time unspecified (not a known event date)", language)));
    }
    const day = Date.UTC(2026, 10, 8) / 1000;
    const dayHtml = renderToStaticMarkup(MemoryEvidence({ item: { ...fact, kind: "episodic", episode_time_source: "explicit", episode_time_precision: "day", occurred_at: day } }));
    const occurred = dayHtml.split(`<dt>${translate("Occurred", language)}</dt><dd>`)[1].split("</dd>")[0];
    assert.ok(occurred.includes(localizedDate(day, language, "day")));
    assert.ok(!occurred.includes(localizedDate(day, language)));
    assert.ok(occurred.includes(translate("Explicit event date (UTC; time unspecified)", language)));
  }
});

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...elements(tree.props?.children)];
}

test("Episodic create forms expose occurrence timestamps, clear them on kind changes and preserve explicit payloads", async () => {
  let current;
  let index;
  const scopes = new Map();
  const calls = [];
  let records = [];
  const { MemoryManagement } = loadComponent("memories.tsx", {
    react: { useState: initial => {
      const slot = index++;
      const state = current;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], value => { state[slot] = value; }];
    } },
    "./api": {
      active: () => false, segment: encodeURIComponent,
      useQuery: path => ({ data: { items: records }, dataPath: path, error: "", loading: false }),
      useAction: () => ({ busy: false, error: "", notice: "", run: async (path, body, method) => {
        calls.push({ path, body, method });
        return { id: "accepted", state: "succeeded" };
      } }),
    },
    "./memory-model": model, "./locale-context": { useI18n: () => locale("en") }, "./ui": { Empty: () => null, Feedback: () => null },
  });
  function render(component, props) {
    if (!scopes.has(component)) scopes.set(component, []);
    current = scopes.get(component); index = 0;
    return elements(component(props));
  }
  const panel = render(MemoryManagement, {}).find(node => typeof node.type === "function" && node.type.name === "Memories");
  const nodes = () => render(panel.type, { ...panel.props, enabled: true });
  const occurrence = () => nodes().find(node => node.type === "label" && node.props.children?.[0] === "Occurred at (UTC Unix seconds)");
  const kind = value => nodes().find(node => node.type === "select" && ["semantic", "episodic", "procedural"].includes(node.props.value)).props.onChange({ target: { value } });
  nodes().find(node => node.type === "button" && node.props.children === "Create Memory").props.onClick();
  assert.equal(occurrence(), undefined);
  kind("episodic");
  occurrence().props.children[1].props.onChange({ target: { value: "1800000000" } });
  kind("procedural");
  assert.equal(occurrence(), undefined);
  kind("episodic");
  assert.equal(occurrence().props.children[1].props.value, "");
  occurrence().props.children[1].props.onChange({ target: { value: "1800001234" } });
  nodes().find(node => node.type === "textarea").props.onChange({ target: { value: "The event occurred at a known instant." } });
  await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(calls[0].body.kind, "episodic");
  assert.equal(calls[0].body.occurred_at, 1800001234);
  const day = Date.UTC(2026, 10, 8) / 1000;
  records = [{ ...fact, kind: "episodic", occurred_at: day, episode_time_source: "explicit", episode_time_precision: "day" }];
  nodes().find(node => node.type === "button" && node.props.children === "Edit").props.onClick();
  nodes().find(node => node.type === "textarea").props.onChange({ target: { value: "Only the event description changed." } });
  await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(calls[1].method, "PUT");
  assert.equal(calls[1].body.occurred_at, day);
  assert.equal(calls[1].body.time_precision, "day");
  nodes().find(node => node.type === "button" && node.props.children === "Edit").props.onClick();
  occurrence().props.children[1].props.onChange({ target: { value: String(day + 3600) } });
  await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(calls[2].body.occurred_at, day + 3600);
  assert.equal("time_precision" in calls[2].body, false);
  for (const nonEpisode of ["semantic", "procedural"]) {
    assert.equal(model.memoryPayload({ ...model.memoryInput(), text: "Notes", kind: nonEpisode, occurred_at: 1800001234 }).occurred_at, null);
  }
});

test("Working panel exposes failed reconciliation, retains its draft and blocks replay in both languages", async () => {
  for (const language of ["en", "zh-CN"]) {
    let index = 0;
    const state = [];
    const calls = [];
    const task = { ...model.newWorkingTask("task"), revision: 5, goal: "Saved notes" };
    const { WorkingMemoryPanel } = loadComponent("working-memory.tsx", {
      react: { useState: initial => {
        const slot = index++;
        if (!(slot in state)) state[slot] = initial;
        return [state[slot], value => { state[slot] = value; }];
      } },
      "./api": {
        segment: encodeURIComponent, errorText: error => error.message,
        api: async (path, options) => {
          calls.push({ path, options });
          throw new Error(options ? "Outcome unknown" : "Snapshot read failed");
        },
      },
      "./memory-model": model, "./locale-context": { useI18n: () => locale(language) }, "./ui": { Feedback: () => null },
    });
    const query = { data: { items: [task] }, loading: false, error: "", refresh() {} };
    const render = () => { index = 0; return elements(WorkingMemoryPanel({ sessionId: "session", query, selected: "task", onSelect() {}, disabled: false })); };
    render().find(node => node.type === "button" && node.props.children === translate("Edit", language)).props.onClick();
    render().find(node => node.type === "textarea").props.onChange({ target: { value: "Unsaved 用户 draft" } });
    await render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    const nodes = render();
    const feedback = nodes.find(node => typeof node.type === "function" && node.props.notice);
    assert.ok(feedback.props.error.includes("Outcome unknown"));
    assert.ok(feedback.props.error.includes("Snapshot read failed"));
    assert.equal(feedback.props.notice, "The saved snapshot could not be rechecked. Reload it before writing again; your draft is kept until then.");
    assert.equal(nodes.find(node => node.type === "textarea").props.value, "Unsaved 用户 draft");
    assert.equal(nodes.find(node => node.type === "fieldset").props.disabled, true);
    assert.ok(nodes.some(node => node.type === "button" && node.props.children === translate("Reload saved snapshot", language)));
    await nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    assert.equal(calls.length, 2);
  }
});

test("Settings reports review opt-in and explains its TOML switch in both languages", () => {
  for (const language of ["en", "zh-CN"]) {
    const { MemoryManagement } = loadComponent("memories.tsx", {
      react: { useState: () => [false, () => {}] },
      "./api": {}, "./memory-model": model, "./locale-context": { useI18n: () => locale(language) }, "./ui": {},
    });
    for (const required of [false, true]) {
      const tree = MemoryManagement({ status: { memory_configured: true, memory: { review_required: required, index_state: "ready", pending_index_operations: 0 } } });
      const nodes = elements(tree);
      assert.ok(nodes.some(node => node.type === "strong" && node.props.children === translate(required ? "Required before active recall" : "Automatic activation (default)", language)));
      assert.ok(nodes.some(node => node.type === "code" && node.props.children === "review_required = true"));
      assert.ok(nodes.some(node => node.type === "code" && node.props.children === "[memory]"));
    }
  }
});

test("Chat refreshes working revisions after settling and extraction candidates are shown as awaiting review", async () => {
  let language = "en";
  let index = 0;
  let succeeded = true;
  const state = [];
  const refreshes = [];
  const runs = [];
  const task = { ...model.newWorkingTask("task"), revision: 2, goal: "User notes" };
  const { Knowledge } = loadComponent("knowledge.tsx", {
    ...messageComponents,
    react: {
      useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
      useState: initial => {
        const slot = index++;
        if (!(slot in state)) state[slot] = initial;
        return [state[slot], value => { state[slot] = value; }];
      },
      useRef: () => ({ current: null }), useEffect() {},
    },
    "./locale-context": { useI18n: () => locale(language) },
    "./api": {
      segment: encodeURIComponent, useAction: () => ({ busy: false }),
      useAgentAction: () => ({ busy: false, run: async (...args) => { runs.push(args); return succeeded; } }),
      useQuery: path => ({
        data: path.endsWith("/attachments") ? undefined : path.includes("/working-memory") ? { items: [task] } : path.startsWith("/operations/") ? { state: "succeeded", result: { memories: 0, candidates: 2 } } :
          { items: [{ id: "assistant", role: "assistant", content: "Answer", state: "completed", memory_operation_id: "extraction" }] },
        loading: false, error: "", refresh: () => refreshes.push(path),
      }),
    },
    "./ui": { Empty: () => null, Feedback: () => null, Sources: () => null },
    "./agent-client": { chatTrace() {} },
    "./memory-model": model, "./working-memory": { WorkingMemoryPanel: () => null },
  });
  const chat = elements(Knowledge({ sessionId: "session/a", status: { workspace_id: "workspace", model_configured: true, memory_configured: true }, onSessionChanged() {}, drafts: new SessionDrafts() }))
    .find(node => typeof node.type === "function" && node.type.name === "Chat");
  const render = () => { index = 0; return elements(chat.type(chat.props)); };
  let nodes = render();
  nodes.find(node => typeof node.type === "function" && node.type.name === "WorkingMemoryPanel").props.onSelect("task");
  for (const result of [true, false]) {
    succeeded = result;
    render().find(node => node.type === "textarea").props.onChange({ target: { value: "Question" } });
    await render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    assert.equal(runs.at(-1)[1].working_task_id, "task");
    assert.ok(refreshes.at(-1).endsWith("/working-memory"));
    assert.ok(refreshes.at(-2).includes("/messages"));
  }
  nodes = render();
  const row = nodes.find(node => typeof node.type === "function" && node.type.name === "HistoryMessage");
  const status = elements(row.type(row.props)).find(node => typeof node.type === "function" && node.type.name === "MemoryTaskStatus");
  const heading = elements(row.type(row.props)).find(node => typeof node.type === "function" && node.type.name === "MessageHeading");
  for (language of ["en", "zh-CN"]) {
    const headingHtml = renderToStaticMarkup(heading.type(heading.props));
    assert.match(headingHtml, /<img[^>]*alt=""[^>]*src="\/icon\.svg"/);
    assert.ok(headingHtml.includes(translate("Shovel", language)));
    const userHeadingHtml = renderToStaticMarkup(heading.type({ user: true }));
    assert.ok(userHeadingHtml.includes("<svg"));
    assert.ok(!userHeadingHtml.includes("<img"));
    const html = renderToStaticMarkup(status.type(status.props));
    assert.ok(html.includes(translate("saved awaiting review", language)));
    assert.ok(!html.includes(translate("no durable facts found", language)));
    assert.ok(!html.includes(translate("saved in SQLite", language)));
  }
});

test("Unavailable selected working context blocks Chat without dropping identity; explicit clearing allows sending", async () => {
  for (const language of ["en", "zh-CN"]) {
    let current;
    let index;
    const scopes = new Map();
    const runs = [];
    const task = { ...model.newWorkingTask("chosen-task"), revision: 3, goal: "Chosen context" };
    let working = { data: { items: [task] }, loading: false, error: "" };
    const react = {
      useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
      useState: initial => {
        const slot = index++;
        const state = current;
        if (!(slot in state)) state[slot] = initial;
        return [state[slot], value => { state[slot] = value; }];
      },
      useRef: () => ({ current: null }), useEffect() {},
    };
    const dependencies = {
      ...messageComponents,
      react, "./locale-context": { useI18n: () => locale(language) },
      "./api": {
        segment: encodeURIComponent,
        useAgentAction: () => ({ busy: false, run: async (...args) => { runs.push(args); return true; } }),
        useQuery: path => ({ ...(path.endsWith("/working-memory") ? working : { data: path.endsWith("/attachments") ? undefined : { items: [] }, loading: false, error: "" }), refresh() {} }),
      },
      "./memory-model": model, "./ui": { Empty: () => null, Feedback: () => null, Sources: () => null },
      "./agent-client": { chatTrace() {} },
    };
    const { WorkingMemoryPanel } = loadComponent("working-memory.tsx", dependencies);
    const { Knowledge } = loadComponent("knowledge.tsx", { ...dependencies, "./working-memory": { WorkingMemoryPanel } });
    function render(component, props) {
      if (!scopes.has(component)) scopes.set(component, []);
      current = scopes.get(component); index = 0;
      return elements(component(props));
    }
    const chat = render(Knowledge, { sessionId: "session", status: { workspace_id: "workspace", model_configured: true, memory_configured: true }, onSessionChanged() {}, drafts: new SessionDrafts() })
      .find(node => typeof node.type === "function" && node.type.name === "Chat");
    const nodes = () => render(chat.type, chat.props);
    const panel = () => {
      const element = nodes().find(node => node.type === WorkingMemoryPanel);
      return render(element.type, element.props);
    };
    const send = () => nodes().find(node => node.type === "button" && node.props.children === translate("Send Message", language));
    const enterQuestion = () => nodes().find(node => node.type === "textarea").props.onChange({ target: { value: "Question with chosen context" } });
    for (const failure of [
      { data: { items: [task] }, loading: false, error: "Task read failed" },
      { data: { items: [{ ...task, state: "completed" }] }, loading: false, error: "" },
      { data: { items: [{ ...task, state: "expired" }] }, loading: false, error: "" },
      { data: { items: [] }, loading: false, error: "" },
    ]) {
      working = { data: { items: [task] }, loading: false, error: "" };
      panel().find(node => node.type === "select").props.onChange({ target: { value: "chosen-task" } });
      enterQuestion();
      working = failure;
      assert.equal(send().props.disabled, true);
      assert.ok(nodes().some(node => node.props?.role === "alert"));
      const select = panel().find(node => node.type === "select");
      assert.equal(select.props.value, "chosen-task");
      assert.equal(select.props.disabled, false);
      const unavailable = elements(select).find(node => node.type === "option" && node.props.value === "chosen-task");
      assert.equal(unavailable.props.disabled, true);
      assert.equal(unavailable.props.children, translate("Unavailable working task: {p0}", language, { p0: "chosen-task" }));
      const previous = runs.length;
      await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
      assert.equal(runs.length, previous);
      select.props.onChange({ target: { value: "" } });
      assert.equal(send().props.disabled, false);
      await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
      assert.equal(runs.at(-1)[1].working_task_id, null);
    }
    working = { data: undefined, loading: false, error: "Task read failed" };
    enterQuestion();
    assert.equal(send().props.disabled, false);
    await nodes().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
    assert.equal(runs.at(-1)[1].working_task_id, null);
  }
});

test("Expired working snapshots are inspectable and deletable but never selected, saved or revived", async () => {
  let index = 0;
  const state = [];
  const calls = [];
  const task = { ...model.newWorkingTask("expired-task"), revision: 9, goal: "Expired notes", state: "expired" };
  const query = { data: { items: [task] }, loading: false, error: "", refresh() {} };
  const { WorkingMemoryPanel } = loadComponent("working-memory.tsx", {
    react: { useState: initial => {
      const slot = index++;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], value => { state[slot] = value; }];
    } },
    "./api": { segment: encodeURIComponent, errorText: error => error.message, api: async (path, options) => { calls.push({ path, options }); return { deleted: true }; } },
    "./memory-model": model, "./locale-context": { useI18n: () => locale("en") }, "./ui": { Feedback: () => null },
  });
  const render = () => { index = 0; return elements(WorkingMemoryPanel({ sessionId: "session", query, selected: "", onSelect() {}, disabled: false })); };
  assert.equal(model.selectedWorkingTask([task], task.task_id), null);
  assert.throws(() => model.workingPayload(task), /Expired working tasks/);
  render().find(node => node.type === "button" && node.props.children === "Inspect notes").props.onClick();
  let nodes = render();
  assert.equal(nodes.find(node => node.type === "textarea").props.readOnly, true);
  assert.equal(nodes.find(node => node.type === "button" && node.props.children === "Save working notes").props.disabled, true);
  await nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(calls.length, 0);
  nodes.find(node => node.type === "button" && node.props.children === "Delete working task…").props.onClick();
  await render().find(node => node.type === "button" && node.props.children === "Confirm delete").props.onClick();
  assert.equal(calls[0].options.method, "DELETE");
  assert.ok(calls[0].path.endsWith("/expired-task?revision=9"));
});

test("Memory management queues scoped create/review actions and waits for the Operation before refreshing", async () => {
  let current;
  let index;
  let refreshed = 0;
  let receipt;
  const scopes = new Map();
  const calls = [];
  const queries = [];
  const memories = { items: [fact, { ...fact, id: "candidate", state: "candidate" }] };
  const action = {
    busy: false, error: "", notice: "", setNotice() {},
    run: async (path, body, method) => {
      calls.push({ path, body, method });
      return receipt = { id: `operation-${calls.length}`, state: "queued", result: {}, error: null };
    },
  };
  const { MemoryManagement } = loadComponent("memories.tsx", {
    react: {
      useState: initial => {
        const slot = index++;
        const state = current;
        if (!(slot in state)) state[slot] = initial;
        return [state[slot], value => { state[slot] = typeof value === "function" ? value(state[slot]) : value; }];
      },
      useRef: initial => {
        const slot = index++;
        if (!(slot in current)) current[slot] = { current: initial };
        return current[slot];
      },
      useEffect: callback => callback(),
    },
    "./api": {
      active: state => ["queued", "running"].includes(state), segment: encodeURIComponent,
      useAction: () => action,
      useQuery: path => {
        queries.push(path);
        return { data: path?.startsWith("/operations/") ? receipt : memories, dataPath: path, loading: false, error: "", refresh: () => refreshed++ };
      },
    },
    "./memory-model": model, "./locale-context": { useI18n: () => locale("en") }, "./ui": { Feedback: () => null, Empty: () => null },
  });
  function render(component, props) {
    if (!scopes.has(component)) scopes.set(component, []);
    current = scopes.get(component); index = 0;
    return component(props);
  }
  const parent = render(MemoryManagement, { status: { memory_configured: true }, sessionId: "session/a" });
  const component = elements(parent).find(node => typeof node.type === "function" && node.type.name === "Memories");
  const props = { ...component.props, enabled: true };
  const panel = () => elements(render(component.type, props));
  let nodes = panel();
  assert.ok(queries.includes("/memories?include_history=true&conversation_id=session%2Fa"));
  assert.equal(nodes.filter(node => node.type === "button" && node.props.children === "Approve candidate…").length, 1);
  nodes.find(node => node.type === "button" && node.props.children === "Create Memory").props.onClick();
  nodes = panel();
  nodes.find(node => node.type === "textarea").props.onChange({ target: { value: "手动事实" } });
  await panel().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.deepEqual(calls[0], {
    path: "/memories", method: "POST", body: { ...model.memoryInput(), text: "手动事实" },
  });
  nodes = panel();
  assert.equal(nodes.find(node => node.type === "button" && node.props.children === "Create Memory").props.disabled, true);
  assert.equal(refreshed, 0);
  const monitor = nodes.find(node => typeof node.type === "function" && node.type.name === "MemoryOperation");
  render(monitor.type, monitor.props);
  assert.equal(refreshed, 0);
  receipt = { ...receipt, state: "succeeded" };
  render(monitor.type, monitor.props);
  assert.equal(refreshed, 1);
  nodes = panel();
  assert.equal(nodes.find(node => node.type === "button" && node.props.children === "Create Memory").props.disabled, false);
  nodes.find(node => node.type === "button" && node.props.children === "Approve candidate…").props.onClick();
  await panel().find(node => node.type === "button" && node.props.children === "Confirm approve").props.onClick();
  assert.deepEqual(calls[1], {
    path: "/memories/candidate/review", body: { decision: "approve", conversation_id: "session/a" }, method: undefined,
  });
});

test("Working editor keeps drafts across language/query refreshes, selects only active tasks and reconciles CAS failures", async () => {
  let language = "en";
  let index = 0;
  const state = [];
  const calls = [];
  const selected = [];
  let refreshed = 0;
  const task = { ...model.newWorkingTask("task/a"), revision: 3, goal: "Original goal" };
  const query = { data: { items: [task, { ...task, task_id: "finished", state: "completed" }] }, loading: false, error: "", refresh: () => refreshed++ };
  const dependencies = {
    react: { useState: initial => {
      const slot = index++;
      if (!(slot in state)) state[slot] = initial;
      return [state[slot], value => { state[slot] = typeof value === "function" ? value(state[slot]) : value; }];
    } },
    "./api": {
      segment: encodeURIComponent, errorText: error => error.message,
      api: async (path, options) => {
        calls.push({ path, options });
        if (options) throw new Error("Revision conflict");
        return { items: [{ ...task, revision: 4, goal: "Server goal" }] };
      },
    },
    "./memory-model": model, "./locale-context": { useI18n: () => locale(language) },
    "./ui": { Feedback: () => null },
  };
  const { WorkingMemoryPanel } = loadComponent("working-memory.tsx", dependencies);
  const render = () => { index = 0; return WorkingMemoryPanel({ sessionId: "session/a", query, selected: "task/a", onSelect: value => selected.push(value), disabled: false }); };
  let nodes = elements(render());
  assert.deepEqual(nodes.filter(node => node.type === "option").map(node => node.props.value), ["", "task/a"]);
  nodes.find(node => node.type === "select").props.onChange({ target: { value: "task/a" } });
  assert.deepEqual(selected, ["task/a"]);
  nodes.find(node => node.type === "button" && node.props.children === "Edit").props.onClick();
  nodes = elements(render());
  nodes.find(node => node.type === "textarea" && node.props.value === "Original goal").props.onChange({ target: { value: "Unsaved 用户 draft" } });
  language = "zh-CN";
  query.data = { items: [{ ...task, revision: 4, goal: "Poll result" }] };
  nodes = elements(render());
  assert.ok(nodes.some(node => node.type === "textarea" && node.props.value === "Unsaved 用户 draft"));
  await nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(JSON.parse(calls[0].options.body).expected_revision, 3);
  assert.equal(JSON.parse(calls[0].options.body).goal, "Unsaved 用户 draft");
  assert.equal(calls[1].options, undefined);
  nodes = elements(render());
  assert.equal(nodes.find(node => node.type === "fieldset").props.disabled, true);
  assert.ok(nodes.some(node => node.type === "textarea" && node.props.value === "Unsaved 用户 draft"));
  await nodes.find(node => node.type === "button" && node.props.children === translate("Reload saved snapshot", language)).props.onClick();
  nodes = elements(render());
  assert.ok(nodes.some(node => node.type === "textarea" && node.props.value === "Server goal"));
  assert.equal(nodes.find(node => node.type === "fieldset").props.disabled, false);
  assert.equal(refreshed, 2);
});
