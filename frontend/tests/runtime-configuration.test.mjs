import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { translate } from "../app/i18n.ts";
import { configurationChanges, configurationDirty, configurationDraft } from "../app/runtime-configuration-model.ts";

const require = createRequire(import.meta.url);
const fields = [
  { key: "http_port", section: "", type: "number", value: 8080, active_value: 8080 },
  { key: "model.security_key", section: "model", type: "string", secret: true, value: null, configured: true },
  { key: "agent.enable_python_extensions", section: "agent", type: "boolean", value: false },
  { key: "agent.workspace_path", section: "agent", type: "string", value: "fixed-workspace", readonly: true },
  { key: "embedding.dimensions", section: "embedding", type: "number", value: null, nullable: true },
].map(field => ({ choices: null, minimum: null, maximum: null, environment: [], ...field }));
const original = { path: "fixed-profile-settings.toml", revision: "a".repeat(64), items: fields, applied: true, editable: true, pending: [], application: "host_restart" };

function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...elements(tree.props?.children)];
}

function harness() {
  const state = [];
  let index = 0, language = "en";
  const requests = [];
  const action = {
    busy: false, error: "", notice: "", fail: false,
    setNotice(value) { action.notice = value; },
    async run(path, body, method) {
      requests.push({ path, body, method });
      if (action.fail) { action.error = "Configuration changed. Reload the saved file before saving."; return; }
      if (method === "GET") return original;
      if (path.endsWith("/check")) return { ...original, applied: false, pending: ["http_port"] };
      return {
        ...original, revision: "b".repeat(64), applied: false, pending: ["http_port"],
        items: fields.map(field => ({ ...field, value: body.changes[field.key] ?? field.value })),
      };
    },
  };
  const dependencies = {
    react: {
      ...require("react"),
      useState(initial) {
        const slot = index++;
        if (!(slot in state)) state[slot] = typeof initial === "function" ? initial() : initial;
        return [state[slot], value => { state[slot] = typeof value === "function" ? value(state[slot]) : value; }];
      },
    },
    "./api": { useAction: () => action, errorText: error => error.message },
    "./locale-context": { useI18n: () => ({ t: key => translate(key, language) }) },
    "./runtime-configuration-model": { configurationChanges, configurationDirty, configurationDraft },
    "./ui": { Feedback: () => null },
  };
  const output = ts.transpileModule(readFileSync(new URL("../app/runtime-configuration.tsx", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function("require", "exports", output)(id => dependencies[id] ?? require(id), exports);
  return {
    requests, action,
    language(value) { language = value; },
    render() { index = 0; return elements(exports.ConfigurationForm({ initial: original })); },
  };
}
function field(ui, key) { return ui.render().find(node => node.props.id === `runtime-${key}`); }
function button(ui, label) { return ui.render().find(node => node.type === "button" && node.props.children === label); }
async function submit(ui) { await ui.render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} }); }

test("Only changed allowlisted scalar values are sent, with distinct secret keep/replace/clear semantics", () => {
  const draft = configurationDraft(fields);
  assert.deepEqual(configurationChanges(fields, draft, {}), {});
  assert.equal(configurationDirty(fields, draft, {}), false);
  draft.http_port = "8081";
  draft["agent.enable_python_extensions"] = "true";
  draft["agent.workspace_path"] = "cannot-change";
  draft["embedding.dimensions"] = "1024";
  assert.deepEqual(configurationChanges(fields, draft, {}), {
    http_port: 8081, "agent.enable_python_extensions": true, "embedding.dimensions": 1024,
  });
  draft["model.security_key"] = "new-secret";
  assert.equal(configurationChanges(fields, draft, { "model.security_key": "replace" })["model.security_key"], "new-secret");
  assert.equal(configurationChanges(fields, draft, { "model.security_key": "clear" })["model.security_key"], "");
  assert.equal(configurationChanges(fields, draft, { "model.security_key": "keep" })["model.security_key"], undefined);
});

test("Empty replacement and invalid numeric drafts cannot be converted into silent resets", () => {
  const draft = configurationDraft(fields);
  assert.throws(() => configurationChanges(fields, draft, { "model.security_key": "replace" }), /replacement/);
  for (const value of ["", " ", "NaN", "Infinity"]) {
    draft.http_port = value;
    assert.throws(() => configurationChanges(fields, draft, {}), /valid number/);
  }
  draft.http_port = "8080";
  draft["embedding.dimensions"] = "1024";
  const configured = fields.map(field => field.key === "embedding.dimensions" ? { ...field, value: 1024 } : field);
  draft["embedding.dimensions"] = "";
  assert.equal(configurationChanges(configured, draft, {})["embedding.dimensions"], null);
});

test("Edits survive language changes; save uses the loaded revision without replaying saved keys or fixed paths", async () => {
  const ui = harness();
  field(ui, "http_port").props.onChange({ target: { value: "8081" } });
  ui.language("zh-CN");
  assert.equal(field(ui, "http_port").props.value, "8081");
  assert.ok(button(ui, "保存配置"));
  assert.equal(field(ui, "agent.workspace_path").props.readOnly, true);
  await submit(ui);
  assert.deepEqual(ui.requests[0], {
    path: "/runtime/configuration", method: "PUT", body: { revision: original.revision, changes: { http_port: 8081 } },
  });
  assert.equal(field(ui, "http_port").props.value, "8081");
  assert.ok(ui.action.notice.includes("saved"));
  assert.equal(button(ui, "保存配置").props.disabled, true);
});

test("Explicit secret replacement uses a password input and clears browser drafts after save", async () => {
  const ui = harness();
  const key = "model.security_key";
  field(ui, key).props.onChange({ target: { value: "replace" } });
  const input = ui.render().find(node => node.props.id === `runtime-${key}-replacement`);
  assert.equal(input.props.type, "password");
  input.props.onChange({ target: { value: "new-key" } });
  await submit(ui);
  assert.equal(ui.requests[0].body.changes[key], "new-key");
  assert.equal(field(ui, key).props.value, "keep");
  assert.ok(!ui.render().some(node => node.props.value === "new-key"));
  field(ui, key).props.onChange({ target: { value: "clear" } });
  await submit(ui);
  assert.equal(ui.requests[1].body.changes[key], "");
});

test("Failed saves retain drafts; reload requires discard and checks never claim a pending change was applied", async () => {
  const ui = harness();
  field(ui, "http_port").props.onChange({ target: { value: "8082" } });
  ui.action.fail = true;
  await submit(ui);
  assert.equal(field(ui, "http_port").props.value, "8082");
  assert.equal(button(ui, "Check application").props.disabled, true);
  ui.action.error = "http_port: Port must be between 1 and 65535.";
  ui.language("zh-CN");
  assert.ok(ui.render().some(node => node.props.role === "alert" && node.props.children === "端口必须介于 1 和 65535 之间。"));
  ui.language("en");
  button(ui, "Reload saved file").props.onClick();
  assert.equal(ui.requests.length, 1);
  ui.action.fail = false;
  await button(ui, "Discard edits and reload").props.onClick();
  assert.equal(field(ui, "http_port").props.value, "8080");
  await button(ui, "Check application").props.onClick();
  assert.match(ui.action.notice, /^Not applied:/);
  assert.equal(ui.requests.at(-1).path, "/runtime/configuration/check");
});

test("Runtime configuration retains active provider, warmup and capacity monitors with explicit refresh", () => {
  let refreshed = 0;
  const active = {
    model_configured: true, model_name: "active-model", memory_configured: true, embedding_configured: false,
    chat_capacity: { running: 2, limit: 3, waiting: 1 }, warmup: { index: { state: "ready" } },
  };
  const Editor = () => null;
  const dependencies = {
    react: { ...require("react"), useState: initial => [initial, () => {}], useRef: () => ({ current: [] }) },
    "./api": { useQuery: () => ({ data: active, error: "", refresh() { refreshed++; } }) },
    "./locale-context": { useI18n: () => ({ t: key => key }) },
    "./runtime-configuration": { RuntimeConfigurationEditor: Editor },
    "./ui": { Feedback: () => null },
  };
  const output = ts.transpileModule(readFileSync(new URL("../app/settings.tsx", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  new Function("require", "exports", output)(id => dependencies[id] ?? (id.startsWith("./") ? {} : require(id)), exports);
  const runtime = elements(exports.Settings({})).find(node => node.type?.name === "RuntimeConfiguration");
  const nodes = elements(runtime.type(runtime.props));
  assert.ok(nodes.some(node => node.props.children === "active-model"));
  assert.ok(nodes.some(node => node.props.children === "Performance & warmup"));
  assert.ok(nodes.some(node => node.props.children === "ready"));
  assert.ok(nodes.some(node => node.props.className === "chat-capacity"));
  assert.ok(nodes.some(node => node.type === Editor));
  nodes.find(node => node.props.children === "Refresh runtime status").props.onClick();
  assert.equal(refreshed, 1);
});
