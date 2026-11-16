import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { translate } from "../app/i18n.ts";

const require = createRequire(import.meta.url);
function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...elements(tree.props?.children)];
}
const server = { name: "Literal server #1", enabled: true, transport: "http", auth_type: "oauth_user", credential_refs: [], connection_state: "not_connected" };
const configuration = { path: "C:\\workspace\\mcp.json", content: '{"mcpServers":{}}', revision: "a".repeat(64), exists: true, items: [server],
  warnings: [], validation_error: null, agent_loaded: false, agent_state: "not_loaded", restart_required: false };
const initialStatus = { state: "not_authorized", auth_type: "oauth_user", revision: configuration.revision, expires_at: null, authorization_id: null };
const start = { authorization_id: "auth-1", authorization_url: "https://identity.example/authorize?state=synthetic-state", expires_at: 1999999999,
  issuer: "https://identity.example", scopes: ["tools.read"] };

function harness(initial = configuration) {
  const states = new Map();
  const configQuery = { data: initial, refresh() {} };
  const query = { data: undefined, dataPath: undefined, error: "", refresh() {} };
  const requests = [], reads = [], effects = [];
  let state, index, language = "en", result, lastPath;
  const action = { busy: false, error: "", notice: "", setNotice(value) { action.notice = value; },
    async run(path, body, method) {
      requests.push({ path, body, method });
      return result;
    } };
  const react = { ...require("react"),
    useState(initial) {
      const slot = index++, current = state;
      if (!(slot in current)) current[slot] = typeof initial === "function" ? initial() : initial;
      return [current[slot], value => { current[slot] = typeof value === "function" ? value(current[slot]) : value; }];
    },
    useEffect(effect) { effects.push(effect); },
  };
  const { outputText } = ts.transpileModule(readFileSync(new URL("../app/mcp.tsx", import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const dependencies = { react,
    "./api": { segment: encodeURIComponent, useAction: () => action,
      useQuery(path) {
        reads.push(path);
        if (path === "/agent/mcp" || path === null) return path === "/agent/mcp" ? configQuery : query;
        lastPath = path;
        return query;
      } },
    "./locale-context": { useI18n: () => ({ t: (key, values) => translate(key, language, values), date: value => `date:${value}` }) },
  };
  const exports = {};
  new Function("require", "exports", outputText)(id => dependencies[id] ?? (id.startsWith("./") ? {} : require(id)), exports);
  function render(component, props = {}) {
    if (!states.has(component)) states.set(component, []);
    state = states.get(component); index = 0;
    return elements(component(props));
  }
  const editor = render(exports.MCPManagement).find(node => node.type?.name === "MCPEditor");
  function editorNodes() { return render(editor.type, { ...editor.props, initial: configQuery.data }); }
  const auth = editorNodes().find(node => node.type?.name === "MCPAuthorizationControls");
  function authorization(props = {}) { return render(auth.type, { ...auth.props, ...props }); }
  function acceptStatus(status = initialStatus) { query.data = status; query.dataPath = lastPath; }
  return { render, editorNodes, authorization, editor, action, query, configQuery, requests, reads, effects, acceptStatus,
    result(value) { result = value; }, language(value) { language = value; } };
}
function button(nodes, label) { return nodes.find(node => node.type === "button" && node.props.children === label); }

test("Explicit user authorization uses the saved revision, fences old polls and opens only a matching safe link", async () => {
  const ui = harness();
  ui.authorization(); ui.acceptStatus();
  assert.equal(ui.requests.length, 0);
  ui.result(start);
  await button(ui.authorization(), "Start authorization").props.onClick();
  assert.deepEqual(ui.requests[0], { path: `/agent/mcp/${encodeURIComponent(server.name)}/auth/start`,
    body: { revision: configuration.revision }, method: undefined });
  assert.equal(ui.authorization().some(node => node.type === "a"), false);
  assert.equal(button(ui.authorization(), "Start authorization").props.disabled, true);
  ui.acceptStatus({ ...initialStatus, state: "authorizing", authorization_id: "auth-1" });
  const link = ui.authorization().find(node => node.type === "a");
  assert.equal(link.props.href, start.authorization_url);
  assert.equal(link.props.target, "_blank");
  assert.equal(link.props.rel, "noopener noreferrer");
  assert.equal(link.props.referrerPolicy, "no-referrer");
  ui.acceptStatus({ ...initialStatus, state: "authorizing", authorization_id: "different-flow" });
  assert.equal(ui.authorization().some(node => node.type === "a"), false);
  await button(ui.authorization(), "Cancel authorization").props.onClick();
  assert.equal(ui.requests[1].body.authorization_id, "different-flow");
  assert.equal(ui.requests[1].body.revision, configuration.revision);
  assert.equal(ui.authorization().some(node => node.type === "a"), false);
});

test("Disconnect requires confirmation, reports unsupported revocation and preserves failures for repair", async () => {
  const ui = harness();
  ui.authorization(); ui.acceptStatus({ ...initialStatus, state: "authorized", expires_at: 1700000000 });
  button(ui.authorization(), "Disconnect authorization").props.onClick();
  assert.equal(ui.requests.length, 0);
  ui.result({ state: "not_authorized", local_cleared: true, provider_revocation: "unsupported" });
  await button(ui.authorization(), "Confirm disconnect").props.onClick();
  assert.ok(ui.requests[0].path.endsWith("/disconnect"));
  assert.match(ui.action.notice, /tokens may remain valid/);
  ui.authorization(); ui.acceptStatus();
  button(ui.authorization(), "Disconnect authorization").props.onClick();
  ui.result(undefined); ui.action.error = "Local MCP authorization cleared; provider revocation failed.";
  await button(ui.authorization(), "Confirm disconnect").props.onClick();
  assert.ok(ui.authorization().some(node => node.props.error?.includes("provider revocation failed")));
});

test("Application and disabled servers never offer browser login; malformed authorization URLs stay inert", async () => {
  const ui = harness();
  ui.authorization(); ui.acceptStatus();
  assert.equal(button(ui.authorization({ server: { ...server, auth_type: "oauth_client_credentials" } }), "Start authorization"), undefined);
  const disabled = ui.authorization({ server: { ...server, enabled: false } });
  assert.equal(ui.reads.at(-1), null);
  assert.equal(button(disabled, "Start authorization").props.disabled, true);
  assert.equal(button(ui.authorization({ blocked: true }), "Start authorization").props.disabled, true);
  ui.result({ ...start, authorization_url: "javascript:alert(1)" });
  await button(ui.authorization(), "Start authorization").props.onClick();
  ui.authorization(); ui.acceptStatus({ ...initialStatus, state: "authorizing", authorization_id: "auth-1" });
  assert.equal(ui.authorization().some(node => node.type === "a"), false);
  assert.ok(ui.authorization().some(node => node.props.error === "Shovel returned an invalid authorization URL."));
});

test("Authentication status and locale changes preserve literal configuration drafts and show distinct connection failures", async () => {
  const ui = harness();
  const draft = '{"mcpServers":{"user-content":"Not translated"}}';
  ui.editorNodes().find(node => node.type === "textarea").props.onChange({ target: { value: draft } });
  ui.language("zh-CN");
  ui.configQuery.data = { ...configuration, items: [{ ...server, connection_state: "permission_denied" }], agent_loaded: true, agent_state: "loaded" };
  let nodes = ui.editorNodes();
  assert.equal(nodes.find(node => node.type === "textarea").props.value, draft);
  assert.ok(nodes.some(node => node.props.children === "权限不足（403）"));
  assert.ok(nodes.some(node => node.props.children === server.name));
  assert.equal(nodes.find(node => node.type?.name === "MCPAuthorizationControls").props.blocked, true);
  ui.result(undefined); ui.action.error = "MCP configuration changed; reload before saving.";
  await nodes.find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(ui.editorNodes().find(node => node.type === "textarea").props.value, draft);
  assert.equal(ui.requests[0].body.revision, configuration.revision);
  ui.configQuery.data = { ...configuration, items: [{ ...server, connection_state: "authentication_failed" }] };
  nodes = ui.editorNodes();
  assert.ok(nodes.some(node => node.props.children === "认证失败（401）"));
});

test("Credential fields are empty and write-only, send only configured references and clear acknowledged new secrets", async () => {
  const ui = harness({ ...configuration, items: [{ ...server, credential_refs: ["keyring:fixture", "env:MCP_TOKEN"] }] });
  const credential = ui.editorNodes().find(node => node.type?.name === "MCPCredentialEditor");
  const render = props => ui.render(credential.type, { ...credential.props, ...props });
  assert.equal(render().find(node => node.type === "input").props.value, "");
  const input = render().find(node => node.type === "input");
  assert.equal(input.props.type, "password");
  assert.equal(input.props.autoComplete, "new-password");
  input.props.onChange({ target: { value: "synthetic-private-value" } });
  ui.language("zh-CN");
  assert.equal(render().find(node => node.type === "input").props.value, "synthetic-private-value");
  ui.result({ saved: true });
  await render().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.deepEqual(ui.requests[0], { path: `/agent/mcp/${encodeURIComponent(server.name)}/credentials`, method: "PUT",
    body: { revision: configuration.revision, reference: "keyring:fixture", value: "synthetic-private-value" } });
  assert.equal(render().find(node => node.type === "input").props.value, "");
  assert.equal(render({ reference: "env:MCP_TOKEN" }).some(node => node.type === "input"), false);
  assert.equal(render({ blocked: true }).find(node => node.type === "input").props.disabled, true);
});
