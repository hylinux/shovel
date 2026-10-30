export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export type Permission = { action: string; resource: string };
export type CapabilityKind = "function" | "skill" | "workflow" | "agent";
export type Reference = { capability_id: string; version: number };
export type LockedReference = Reference & { content_hash: string };
export type Binding = { source: "literal" | "context" | "event"; path?: string[]; value?: Json };
export type Condition = { path: string[]; operator: "eq" | "ne" | "contains" | "in" | "exists" | "gt" | "gte" | "lt" | "lte"; value: Json };
export const nodeKinds = ["input", "skill", "function", "condition", "approval", "ask_user", "notification", "emit_event", "output"] as const;
export type NodeKind = typeof nodeKinds[number];
export type WorkflowNode = { id: string; kind: NodeKind; capability?: Reference | null; input_mapping?: Record<string, Binding>; condition?: Condition | null; permission_boundary?: Permission[] | null; prompt?: string; response_schema?: JsonObject; event_type?: string | null };
export type WorkflowEdge = { source: string; target: string; label?: "next" | "true" | "false" | "error" };
export type Implementation = { kind: "skill" | "agent"; instructions: string } | { kind: "function"; implementation_id: string } | { kind: "workflow"; nodes: WorkflowNode[]; edges: WorkflowEdge[] };
export type Content = {
  implementation: Implementation; input_schema: JsonObject; output_schema: JsonObject;
  required_permissions: Permission[]; permission_boundary: Permission[];
  risk_profile: { level: "low" | "medium" | "high" | "critical"; reasons: string[] };
  side_effect_profile: { kind: "none" | "local_write" | "external_write" | "destructive"; description: string };
  approval_requirement: "platform_minimum" | "always"; allowed_capabilities: Reference[];
  dependency_lock: { format_version: 1; dependencies: LockedReference[] };
  connection_requirements: { name: string; connection_ref: string }[]; provenance: JsonObject;
};
export type Definition = { id: string; kind: CapabilityKind; name: string; description: string; availability: string };
export type Version = { capability_id: string; id: string; number: number; revision: number; content: Content; availability: string; published_at: number | null; content_hash: string | null };
export type CapabilityOption = { definition: Definition; version: Version };
export type PlatformOptions = { permissions: Permission[]; items: CapabilityOption[]; memory_access: "disabled" };
export type ApprovalPreference = "ALWAYS" | "RISK_BASED" | "EXTERNAL_ACTIONS" | "MINIMUM_REQUIRED";
export const approvalPreferences: { value: ApprovalPreference; label: string }[] = [
  { value: "ALWAYS", label: "Always ask me" },
  { value: "RISK_BASED", label: "Ask based on risk" },
  { value: "EXTERNAL_ACTIONS", label: "Ask before external or sensitive actions" },
  { value: "MINIMUM_REQUIRED", label: "Don't ask unless Shovel requires approval" },
];
export type Schedule = { kind: "interval" | "daily"; timezone: string; anchor_at: number; interval_seconds: number | null; hour: number | null; minute: number | null; missed_policy: "skip" | "fire_once"; misfire_grace_seconds: number };
export type Trigger = { kind: "manual" } | { kind: "event"; event_type: string; source: string | null } | { kind: "schedule"; schedule: Schedule };
export type ProactiveConfiguration = {
  trigger: Trigger; conditions: Condition[]; target: { capability_id: string; version_policy: "exact" | "latest_published"; version: number | null };
  input_mapping: Record<string, Binding>; permission_boundary: Permission[]; approval_preference: ApprovalPreference;
  notification_policy: "none" | "failures" | "all"; quiet_hours: { timezone: string; start_minute: number; end_minute: number } | null; cooldown_seconds: number;
};
export type ProactiveService = {
  id: string; name: string; description: string; enabled: boolean; paused: boolean; revision: number;
  configuration: ProactiveConfiguration; target_name?: string; target_kind?: CapabilityKind;
  resolved_version?: number | null; target_error?: string | null; last_activity?: number | null; next_trigger?: number | null;
};
export type AutomationControl = { paused: boolean; revision: number; resumed_at: number };
export type ServiceControlState = Pick<ProactiveService, "id" | "revision" | "enabled" | "paused">;
export function serviceControlState(value: unknown): ServiceControlState | undefined {
  if (typeof value !== "object" || value === null
    || !("id" in value) || typeof value.id !== "string"
    || !("revision" in value) || typeof value.revision !== "number"
    || !("enabled" in value) || typeof value.enabled !== "boolean"
    || !("paused" in value) || typeof value.paused !== "boolean") return;
  return { id: value.id, revision: value.revision, enabled: value.enabled, paused: value.paused };
}
export function observedService(service: ProactiveService, update?: ServiceControlState): ProactiveService {
  return update?.id === service.id && update.revision > service.revision ? { ...service, ...update } : service;
}
export type Execution = {
  id: string; state: string; parent_execution_id: string | null; root_execution_id: string; created_at: number;
  snapshot: { capability: { capability_id: string; number: number; kind: CapabilityKind; content: Content }; context: { source: string; proactive_snapshot: (ProactiveService & { event_id: string }) | null }; granted_permissions: Permission[] };
  output: Json; error: string | null; wait_reason: string | null; duration_seconds: number | null;
  approval_request_id: string | null; attempt_history?: Json[]; checkpoint: JsonObject | null; control: JsonObject;
};
export type Approval = {
  id: string; execution_id: string; user_id: string; state: string; reasons: string[]; expires_at: number;
  capability: Execution["snapshot"]["capability"]; service: ProactiveService | null;
  risk: Content["risk_profile"]; side_effect: Content["side_effect_profile"];
};
export type Interaction = { id: string; execution_id: string; kind: string; source_id: string; message: string; state: string; created_at: number };
export type InputRequest = { id: string; execution_id: string; user_id: string; state: string; prompt: string; response_schema: JsonObject; expires_at: number };
export type PlatformCommand = {
  action: string; identifier?: string; kind?: CapabilityKind; version?: number; expected_revision?: number;
  execution_id?: string; name?: string; description?: string; content?: Content; configuration?: ProactiveConfiguration;
  input?: JsonObject; response?: Json; permissions?: Permission[]; request_key?: string; prepare?: boolean; confirmation_id?: string;
};
export type Confirmation = { confirmation_id: string; expires_at: number; action: string; preview: Json; intent: PlatformCommand };
export const confirmedActions = new Set(["capability.publish", "proactive.enable", "proactive.resume", "automation.resume", "approval.approve", "approval.reject", "interaction.input"]);
export function confirmationCommand(ticket: Confirmation): PlatformCommand {
  return { ...ticket.intent, prepare: false, confirmation_id: ticket.confirmation_id };
}
export function isPlatformRun(body: unknown): body is PlatformCommand {
  return typeof body === "object" && body !== null && "action" in body
    && (body.action === "execution.run" || body.action === "proactive.run");
}
export function jsonObject(text: string, label: string): JsonObject {
  const parsed: Json = JSON.parse(text);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label} must be a JSON object.`);
  return parsed;
}
export function optionKey(reference: Reference) { return `${reference.capability_id}@${reference.version}`; }
export function versionState(version: Version) {
  return version.availability !== "active" ? version.availability : version.published_at === null ? "draft" : "published";
}
export function emptyContent(kind: "skill" | "workflow"): Content {
  return {
    implementation: kind === "skill" ? { kind, instructions: "" } : { kind, nodes: [{ id: "start", kind: "input" }, { id: "end", kind: "output" }], edges: [{ source: "start", target: "end", label: "next" }] },
    input_schema: { type: "object" }, output_schema: { type: "object" },
    required_permissions: [], permission_boundary: [], risk_profile: { level: "low", reasons: [] },
    side_effect_profile: { kind: "none", description: "" }, approval_requirement: "platform_minimum",
    allowed_capabilities: [], dependency_lock: { format_version: 1, dependencies: [] }, connection_requirements: [], provenance: {},
  };
}
export function dependencySelection(keys: string[], options: CapabilityOption[]) {
  const allowed: Reference[] = [];
  const locks = new Map<string, LockedReference>();
  const permissions = new Map<string, Permission>();
  let risk = 0, effect = 0;
  const risks = ["low", "medium", "high", "critical"] as const;
  const effects = ["none", "local_write", "external_write", "destructive"] as const;
  for (const key of new Set(keys)) {
    const option = options.find((item) => optionKey({ capability_id: item.definition.id, version: item.version.number }) === key);
    if (!option || !option.version.content_hash) throw new Error(`Dependency ${key} is unavailable. Reload options or remove it.`);
    const reference = { capability_id: option.definition.id, version: option.version.number };
    allowed.push(reference);
    for (const lock of [...option.version.content.dependency_lock.dependencies, { ...reference, content_hash: option.version.content_hash }]) locks.set(optionKey(lock), lock);
    for (const permission of option.version.content.required_permissions) permissions.set(JSON.stringify(permission), permission);
    risk = Math.max(risk, risks.indexOf(option.version.content.risk_profile.level), effects.indexOf(option.version.content.side_effect_profile.kind));
    effect = Math.max(effect, effects.indexOf(option.version.content.side_effect_profile.kind));
  }
  return { allowed, locks: [...locks.values()], permissions: [...permissions.values()], risk: risks[risk], effect: effects[effect] };
}

export function bindingMap(text: string, label: string): Record<string, Binding> {
  const parsed = jsonObject(text, label);
  const result: Record<string, Binding> = {};
  for (const [key, value] of Object.entries(parsed)) {
    if (value === null || Array.isArray(value) || typeof value !== "object"
      || !["literal", "context", "event"].includes(String(value.source))
      || Object.keys(value).some((field) => !["source", "path", "value"].includes(field))) throw new Error(`${label}: invalid binding for ${key}.`);
    const path = value.path ?? [];
    if (!Array.isArray(path) || !path.every((part): part is string => typeof part === "string" && part.length > 0)) throw new Error(`${label}: ${key} requires a path of strings.`);
    if (value.source === "literal") {
      if (path.length) throw new Error(`${label}: literal bindings cannot have a path.`);
      result[key] = { source: "literal", value: value.value ?? null };
    } else {
      if (!path.length || (value.value !== undefined && value.value !== null) || (value.source !== "context" && value.source !== "event")) throw new Error(`${label}: ${key} requires a reference path, not a value.`);
      result[key] = { source: value.source, path };
    }
  }
  return result;
}
export const conditionOperators: Condition["operator"][] = ["eq", "ne", "contains", "in", "exists", "gt", "gte", "lt", "lte"];
export function conditionList(text: string): Condition[] {
  const parsed: Json = JSON.parse(text);
  if (!Array.isArray(parsed)) throw new Error("Conditions must be a JSON array.");
  return parsed.map((item) => {
    if (item === null || Array.isArray(item) || typeof item !== "object"
      || !Array.isArray(item.path) || !item.path.length
      || !item.path.every((part): part is string => typeof part === "string" && part.length > 0)) throw new Error("Condition requires a non-empty path of strings.");
    const operator = conditionOperators.find((candidate) => candidate === item.operator);
    if (!operator || Object.keys(item).some((key) => !["path", "operator", "value"].includes(key))) throw new Error("Invalid condition operator or field.");
    return { path: item.path, operator, value: item.value ?? null };
  });
}
export type WorkflowGraph = Extract<Content["implementation"], { kind: "workflow" }>;
export function readWorkflowGraph(graph: WorkflowGraph, form: FormData): WorkflowGraph {
  const text = (name: string) => String(form.get(name) || "");
  return {
    ...graph, nodes: graph.nodes.map((node) => ({
      ...node,
      ...(["function", "skill", "notification", "emit_event", "output"].includes(node.kind) ? {
        input_mapping: bindingMap(text(`mapping-${node.id}`), `${node.id} input mapping`),
      } : {}),
      ...(node.kind === "condition" ? { condition: conditionList(JSON.stringify([{
        path: text(`condition-path-${node.id}`).split("."),
        operator: text(`condition-operator-${node.id}`),
        value: JSON.parse(text(`condition-value-${node.id}`)),
      }]))[0] } : {}),
      ...(node.kind === "ask_user" ? { response_schema: jsonObject(text(`response-${node.id}`), `${node.id} response schema`) } : {}),
    })),
  };
}
