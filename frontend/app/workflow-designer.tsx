"use client";

import { useI18n } from "./locale-context";

import { PermissionPicker, RiskSummary } from "./platform";
import { conditionOperators, nodeKinds, optionKey, type Permission, type PlatformOptions, type WorkflowGraph, type WorkflowNode } from "./platform-types";

const labels = { input: "Input", skill: "Skill", function: "Function", condition: "Condition", approval: "Approval", ask_user: "Ask User", notification: "Notification", emit_event: "Emit Event", output: "Output" };

export function WorkflowDesigner({ graph, onChange, options, boundary, disabled }: { graph: WorkflowGraph; onChange: (graph: WorkflowGraph) => void; options: PlatformOptions; boundary: Permission[]; disabled: boolean }) {
  const { t } = useI18n();
  function update(id: string, patch: Partial<WorkflowNode>) {
    onChange({ ...graph, nodes: graph.nodes.map((node) => node.id === id ? { ...node, ...patch } : node) });
  }
  function add(kind: WorkflowNode["kind"]) {
    const node: WorkflowNode = { id: `node_${crypto.randomUUID().slice(0, 8)}`, kind };
    const end = graph.nodes.find((item) => item.kind === "output");
    const incoming = graph.edges.find((edge) => edge.target === end?.id);
    onChange({
      ...graph,
      nodes: [...graph.nodes.filter((item) => item.id !== end?.id), node, ...(end ? [end] : [])],
      edges: incoming && end ? [...graph.edges.filter((edge) => edge !== incoming), { ...incoming, target: node.id }, { source: node.id, target: end.id }] : graph.edges,
    });
  }
  return <section className="full workflow-designer" aria-label={t("Workflow Designer V1")}>
    <h3>{t("Workflow Designer V1")}</h3>
    <p className="muted">{t("Serial DAG only. The palette has no external triggers, scripts, loops, parallel nodes or joins. A Condition requires true and false edges. Function / Skill may have an error edge.")}</p>
    <div className="workflow-palette" role="group" aria-label={t("Node palette")}>{nodeKinds.map((kind) => <button type="button" disabled={disabled || ((kind === "input" || kind === "output") && graph.nodes.some((node) => node.kind === kind))} key={kind} onClick={() => add(kind)}>+ {t(labels[kind])}</button>)}</div>
    <ol className="workflow-canvas" aria-label={t("Workflow graph")}>
      {graph.nodes.map((node) => {
        const selected = options.items.find((item) => node.capability && item.definition.id === node.capability.capability_id && item.version.number === node.capability.version);
        const key = node.capability ? optionKey(node.capability) : "";
        const edges = graph.edges.filter((edge) => edge.source === node.id);
        return <li key={node.id} className={`workflow-node node-${node.kind}`}>
          <div className="title-line"><h4>{t(labels[node.kind])}</h4><code>{node.id}</code>{!["input", "output"].includes(node.kind) && <button type="button" disabled={disabled} onClick={() => onChange({ ...graph, nodes: graph.nodes.filter((item) => item.id !== node.id), edges: graph.edges.filter((edge) => edge.source !== node.id && edge.target !== node.id) })}>{t("Remove node")}</button>}</div>
          <div className="form-grid">
            {["function", "skill"].includes(node.kind) && <>
              <label className="full">{t("Exact ")}{t(labels[node.kind])}{t(" capability")}<select aria-label={t("Exact {p0} capability for {p1}", { p0: t(labels[node.kind]), p1: node.id })} required value={key} disabled={disabled} onChange={(event) => {
                const item = options.items.find((candidate) => `${candidate.definition.id}@${candidate.version.number}` === event.target.value);
                update(node.id, { capability: item ? { capability_id: item.definition.id, version: item.version.number } : null });
              }}><option value="">{t("Select an authorized published version")}</option>{key && !selected && <option value={key}>{t("Unavailable: ")}{key}</option>}{options.items.filter((item) => item.definition.kind === node.kind).map((item) => <option key={`${item.definition.id}@${item.version.number}`} value={`${item.definition.id}@${item.version.number}`}>{item.definition.name}{t(" · v")}{item.version.number}</option>)}</select></label>
              <div className="full"><PermissionPicker label={t("Child permission boundary ({p0})", { p0: node.id })} available={options.permissions.filter((permission) => boundary.some((item) => item.action === permission.action && item.resource === permission.resource))} value={node.permission_boundary ?? boundary} onChange={(permissions) => update(node.id, { permission_boundary: permissions })} disabled={disabled} /></div>
              {selected && <div className="full"><RiskSummary risk={selected.version.content.risk_profile.level} effect={selected.version.content.side_effect_profile.kind} approval={selected.version.content.approval_requirement} /></div>}
            </>}
            {node.kind === "condition" && <>
              <label>{t("Condition context path")}<input name={`condition-path-${node.id}`} required defaultValue={node.condition?.path.join(".") || "input.value"} disabled={disabled} /></label>
              <label>{t("Operator")}<select aria-label={t("Operator for {p0}", { p0: node.id })} name={`condition-operator-${node.id}`} defaultValue={node.condition?.operator || "eq"} disabled={disabled}>{conditionOperators.map((operator) => <option key={operator} value={operator}>{t(operator)}</option>)}</select></label>
              <label className="full">{t("Condition value (JSON)")}<input name={`condition-value-${node.id}`} required defaultValue={JSON.stringify(node.condition?.value ?? null)} disabled={disabled} /></label>
            </>}
            {node.kind === "approval" && <p className="full warning">{t("Mandatory approval. No service preference can bypass this node. Approval cannot grant missing permissions.")}</p>}
            {node.kind === "ask_user" && <>
              <label className="full">{t("Input prompt")}<input required maxLength={4000} value={node.prompt || ""} onChange={(event) => update(node.id, { prompt: event.target.value })} disabled={disabled} /></label>
              <label className="full">{t("Response schema (JSON)")}<textarea aria-label={t("Response schema (JSON) for {p0}", { p0: node.id })} className="json-editor" name={`response-${node.id}`} rows={4} defaultValue={JSON.stringify(node.response_schema || { type: "object" }, null, 2)} disabled={disabled} /></label>
              <p className="full muted">{t("Input is bound to the exact request, Execution and user. Ordinary chat text is not an input response.")}</p>
            </>}
            {node.kind === "notification" && <label className="full">{t("Notification message")}<input value={node.prompt || ""} onChange={(event) => update(node.id, { prompt: event.target.value })} maxLength={4000} disabled={disabled} /></label>}
            {node.kind === "emit_event" && <label className="full">{t("Emitted event type")}<input required value={node.event_type || ""} onChange={(event) => update(node.id, { event_type: event.target.value })} disabled={disabled} /></label>}
            {["function", "skill", "notification", "emit_event", "output"].includes(node.kind) && <label className="full">{t("Input / output mapping (JSON)")}<textarea aria-label={t("Input / output mapping (JSON) for {p0}", { p0: node.id })} className="json-editor" name={`mapping-${node.id}`} rows={4} defaultValue={JSON.stringify(node.input_mapping || {}, null, 2)} disabled={disabled} /><small className="muted">{t("Example: {\"text\":{\"source\":\"context\",\"path\":[\"input\",\"text\"]}}. Prior node outputs use [\"nodes\",\"node_id\",\"field\"].")}</small></label>}
          </div>
          <p className="workflow-outgoing">{edges.length ? edges.map((edge) => `${t(edge.label || "next")} → ${edge.target}`).join(" · ") : node.kind === "output" ? t("End") : t("No outgoing edge")}</p>
        </li>;
      })}
    </ol>
    <fieldset className="workflow-edges"><legend>{t("Edges")}</legend>
      {graph.edges.map((edge, index) => <div className="edge-row" key={index}>
        <label>{t("From")}<select aria-label={t("From edge {p0}", { p0: index + 1 })} value={edge.source} disabled={disabled} onChange={(event) => onChange({ ...graph, edges: graph.edges.map((item, position) => position === index ? { ...item, source: event.target.value } : item) })}>{graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.id}</option>)}</select></label>
        <label>{t("Path")}<select aria-label={t("Path edge {p0}", { p0: index + 1 })} value={edge.label || "next"} disabled={disabled} onChange={(event) => onChange({ ...graph, edges: graph.edges.map((item, position) => position === index ? { ...item, label: event.target.value as typeof edge.label } : item) })}>{["next", "true", "false", "error"].map((label) => <option key={label} value={label}>{t(label)}</option>)}</select></label>
        <label>{t("To")}<select aria-label={t("To edge {p0}", { p0: index + 1 })} value={edge.target} disabled={disabled} onChange={(event) => onChange({ ...graph, edges: graph.edges.map((item, position) => position === index ? { ...item, target: event.target.value } : item) })}>{graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.id}</option>)}</select></label>
        <button type="button" disabled={disabled} onClick={() => onChange({ ...graph, edges: graph.edges.filter((_, position) => position !== index) })}>{t("Remove edge")}</button>
      </div>)}
      <button type="button" disabled={disabled || graph.nodes.length < 2} onClick={() => onChange({ ...graph, edges: [...graph.edges, { source: graph.nodes[0].id, target: graph.nodes.at(-1)!.id, label: "next" }] })}>{t("Add edge")}</button>
    </fieldset>
  </section>;
}
