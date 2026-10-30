import assert from "node:assert/strict";
import test from "node:test";
import { bindingMap, conditionList, confirmationCommand, dependencySelection, emptyContent, observedService, readWorkflowGraph, serviceControlState } from "../app/platform-types.ts";
import { makePendingRequest, pendingRequests, savePendingRequest } from "../app/request-recovery.ts";

test("designer builds exact transitive dependency locks and risk/permission summaries, rejecting missing options", () => {
  const read = { action: "read", resource: "knowledge" };
  const content = { ...emptyContent("skill"), required_permissions: [read], risk_profile: { level: "high", reasons: [] }, dependency_lock: { format_version: 1, dependencies: [{ capability_id: "nested", version: 2, content_hash: "b".repeat(64) }] } };
  const options = [{ definition: { id: "child", kind: "skill" }, version: { number: 3, content_hash: "a".repeat(64), content } }];
  const result = dependencySelection(["child@3", "child@3"], options);
  assert.deepEqual(result.allowed, [{ capability_id: "child", version: 3 }]);
  assert.equal(result.locks.length, 2);
  assert.equal(result.locks.find((row) => row.capability_id === "child").content_hash, "a".repeat(64));
  assert.deepEqual(result.permissions, [read]);
  assert.equal(result.risk, "high");
  assert.throws(() => dependencySelection(["unauthorized@1"], options), /unavailable/);
});

test("workflow form preserves branches and approval while parsing input, output and response mappings", () => {
  const graph = { kind: "workflow", nodes: [{ id: "start", kind: "input" }, { id: "if", kind: "condition" }, { id: "approve", kind: "approval" }, { id: "ask", kind: "ask_user", prompt: "Choose" }, { id: "end", kind: "output" }], edges: [{ source: "if", target: "approve", label: "true" }, { source: "if", target: "end", label: "false" }] };
  const form = new FormData();
  form.set("condition-path-if", "input.count");
  form.set("condition-operator-if", "gte");
  form.set("condition-value-if", "1");
  form.set("response-ask", '{"type":"object"}');
  form.set("mapping-end", '{"result":{"source":"literal","value":false}}');
  const parsed = readWorkflowGraph(graph, form);
  assert.deepEqual(parsed.edges, graph.edges);
  assert.deepEqual(parsed.nodes[1].condition, { path: ["input", "count"], operator: "gte", value: 1 });
  assert.equal(parsed.nodes[2].kind, "approval");
  assert.deepEqual(parsed.nodes.at(-1).input_mapping.result, { source: "literal", value: false });
  form.set("condition-operator-if", "eval");
  assert.throws(() => readWorkflowGraph(graph, form), /Invalid condition/);
});

test("invalid declarative mappings and conditions fail visibly instead of dropping fields", () => {
  assert.throws(() => bindingMap('{"x":{"source":"script","value":"exec"}}', "Mapping"), /invalid binding/);
  assert.throws(() => bindingMap('{"x":{"source":"context","path":[]}}', "Mapping"), /requires a reference/);
  assert.throws(() => bindingMap('{"x":{"source":"literal","value":1,"secret":"no"}}', "Mapping"), /invalid binding/);
  assert.throws(() => conditionList('[{"path":[],"operator":"eq"}]'), /non-empty path/);
  assert.throws(() => conditionList('{"operator":"exists"}'), /JSON array/);
  assert.deepEqual(conditionList('[{"path":["payload","ready"],"operator":"exists"}]'), [{ path: ["payload", "ready"], operator: "exists", value: null }]);
});

test("final confirmation uses exact server intent and opaque ticket, not preview authorization flags", () => {
  const intent = { action: "approval.approve", identifier: "approval-1", execution_id: "execution-1", response: null };
  const result = confirmationCommand({ intent, confirmation_id: "opaque", preview: { approved: true, grants: ["admin"] }, expires_at: 300 });
  assert.deepEqual(result, { ...intent, prepare: false, confirmation_id: "opaque" });
  assert.equal("approved" in result, false);
  assert.equal("grants" in result, false);
});

test("service controls retain the acknowledged server revision while polling still returns an older snapshot", () => {
  const incoming = { id: "service", revision: 3, enabled: true, paused: true, name: "Original" };
  const acknowledged = serviceControlState({ id: "service", revision: 4, enabled: true, paused: false });
  assert.deepEqual(observedService(incoming, acknowledged), { ...incoming, revision: 4, paused: false });
  assert.equal(observedService({ ...incoming, revision: 5 }, acknowledged).revision, 5);
  assert.equal(observedService({ ...incoming, id: "another" }, acknowledged).revision, 3);
  assert.equal(serviceControlState({ id: "execution", revision: 2, state: "queued" }), undefined);
});

test("Execution recovery receipts survive reload and cannot smuggle publish or approvals into resend", async () => {
  const storage = new Map();
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
    get length() { return storage.size; }, key(index) { return [...storage.keys()][index] ?? null; },
    getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storage.set(key, value); },
  } });
  const receipt = await makePendingRequest("workspace", "/platform/commands", "POST", '{"action":"proactive.run","identifier":"service"}');
  savePendingRequest(receipt);
  assert.deepEqual(pendingRequests("workspace"), [receipt]);
  assert.deepEqual(pendingRequests("other"), []);
  savePendingRequest({ ...receipt, body: '{"action":"capability.publish"}' });
  assert.throws(() => pendingRequests("workspace"), /invalid recovery receipt/);
});
