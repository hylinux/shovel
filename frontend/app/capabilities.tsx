"use client";

import { useI18n } from "./locale-context";

import { useState, type FormEvent } from "react";
import { errorText, segment, useQuery, type Items } from "./api";
import { CommandFeedback, JsonField, PermissionPicker, RiskSummary, usePlatformAction } from "./platform";
import { dependencySelection, emptyContent, jsonObject, optionKey, readWorkflowGraph, versionState, type Content, type Definition, type Permission, type PlatformOptions, type Version } from "./platform-types";
import { WorkflowDesigner } from "./workflow-designer";
import { Badge, Empty, Feedback } from "./ui";

export function CapabilityLibrary({ kind, active }: { kind: "skill" | "workflow"; active: boolean }) {
  const { t } = useI18n();
  const query = useQuery<Items<Definition>>(active ? `/platform/capabilities?kind=${kind}` : null, 5000);
  const options = useQuery<PlatformOptions>(active ? "/platform/options" : null);
  const [selected, setSelected] = useState<Definition | "new">();
  function saved(identifier: string) {
    query.refresh(); options.refresh();
    setSelected({ id: identifier, kind, name: "", description: "", availability: "active" });
  }
  return <section className="platform-section">
    <div className="section-toolbar"><div><h2>{kind === "skill" ? t("Skill library") : t("Workflows")}</h2><p>{t("Draft, validate, then explicitly publish an immutable version. Existing executions keep their frozen versions.")}</p></div><button className="primary" disabled={!options.data} onClick={() => setSelected("new")}>{t("New ")}{t(kind)}{t(" draft")}</button></div>
    <Feedback error={query.error || options.error} retry={() => { query.refresh(); options.refresh(); }} />
    {selected && options.data && (selected === "new" ? <CapabilityEditor key={`new-${kind}`} kind={kind} options={options.data} onSaved={saved} onClose={() => setSelected(undefined)} /> : <CapabilityVersions key={selected.id} identifier={selected.id} kind={kind} options={options.data} refresh={() => { query.refresh(); options.refresh(); }} onClose={() => setSelected(undefined)} />)}
    {query.loading && <p role="status">{t("Loading ")}{t(kind)}{t("s...")}</p>}
    {query.data?.items.length === 0 && <Empty title={t("No {p0}s yet", { p0: t(kind) })}>{t("Create a draft. No arbitrary Python, JavaScript or Shell code is accepted.")}</Empty>}
    <div className="capability-library">{query.data?.items.map((definition) => <article className="operation-row" key={definition.id}><div className="title-line"><h3>{definition.name}</h3><Badge state={definition.availability} /></div><p>{definition.description}</p><p className="path">{definition.id}</p><button onClick={() => { setSelected(definition); options.refresh(); }}>{t("Open ")}{t(kind)}{t(" editor")}</button></article>)}</div>
  </section>;
}

function CapabilityVersions({ identifier, kind, options, refresh, onClose }: { identifier: string; kind: "skill" | "workflow"; options: PlatformOptions; refresh: () => void; onClose: () => void }) {
  const { t, date } = useI18n();
  const definition = useQuery<Definition>(`/platform/capabilities/${segment(identifier)}`);
  const versions = useQuery<Items<Version>>(`/platform/capabilities/${segment(identifier)}/versions`);
  const [number, setNumber] = useState<number>();
  const version = versions.data?.items.find((row) => row.number === number) || versions.data?.items[0];
  return <section className="capability-version-panel">
    <Feedback error={definition.error || versions.error} retry={() => { definition.refresh(); versions.refresh(); }} />
    {definition.data && version && <CapabilityEditor key={`${identifier}:${version.number}:${version.published_at ?? "draft"}`} definition={definition.data} version={version} kind={kind} options={options} onSaved={() => { versions.refresh(); definition.refresh(); refresh(); }} onClose={onClose} onForked={(created) => { setNumber(created.number); versions.refresh(); refresh(); }} />}
    <details className="inline-panel" open><summary>{t("Version history")}</summary>
      <div className="version-history">{versions.data?.items.map((row) => <button key={row.id} aria-pressed={version?.number === row.number} onClick={() => setNumber(row.number)}>{t("v")}{row.number} · {versionState(row)}{t(" · r")}{row.revision} · {date(row.published_at)}</button>)}</div>
    </details>
  </section>;
}

function CapabilityEditor({ kind, definition, version, options, onSaved, onClose, onForked }: { kind: "skill" | "workflow"; definition?: Definition; version?: Version; options: PlatformOptions; onSaved: (identifier: string) => void; onClose: () => void; onForked?: (version: Version) => void }) {
  const { t } = useI18n();
  const initial = version?.content || emptyContent(kind);
  const [saved, setSaved] = useState(version);
  const [name, setName] = useState(definition?.name || "");
  const [description, setDescription] = useState(definition?.description || "");
  const [instructions, setInstructions] = useState(initial.implementation.kind === "skill" ? initial.implementation.instructions : "");
  const [inputSchema, setInputSchema] = useState(JSON.stringify(initial.input_schema, null, 2));
  const [outputSchema, setOutputSchema] = useState(JSON.stringify(initial.output_schema, null, 2));
  const [implementation, setImplementation] = useState(initial.implementation);
  const [allowed, setAllowed] = useState(initial.allowed_capabilities.map(optionKey));
  const [permissions, setPermissions] = useState(initial.permission_boundary);
  const [required, setRequired] = useState(initial.required_permissions);
  const [risk, setRisk] = useState(initial.risk_profile.level);
  const [effect, setEffect] = useState(initial.side_effect_profile.kind);
  const [approval, setApproval] = useState(initial.approval_requirement);
  const [dirty, setDirty] = useState(!version);
  const [error, setError] = useState("");
  const [validation, setValidation] = useState<{ valid: boolean; errors: string[] }>();
  const [testInput, setTestInput] = useState("{}");
  const action = usePlatformAction();
  const published = saved?.published_at != null;
  const locked = action.locked || published;
  function content(form: FormData): Content {
    const graph = implementation.kind === "workflow" ? readWorkflowGraph(implementation, form) : undefined;
    const keys = graph ? graph.nodes.flatMap((node) => node.capability ? [optionKey(node.capability)] : []) : allowed;
    const selection = dependencySelection(keys, options.items);
    const ranks = ["low", "medium", "high", "critical"] as const;
    const effects = ["none", "local_write", "external_write", "destructive"] as const;
    const localWrite = graph?.nodes.some((node) => ["notification", "emit_event"].includes(node.kind)) ? 1 : 0;
    const summaryRisk = ranks[Math.max(ranks.indexOf(risk), ranks.indexOf(selection.risk), localWrite)];
    const summaryEffect = effects[Math.max(effects.indexOf(effect), effects.indexOf(selection.effect), localWrite)];
    const requiredPermissions = new Map<string, Permission>([...required, ...selection.permissions].map((item) => [JSON.stringify(item), item]));
    return {
      ...initial, implementation: graph || { kind: "skill", instructions },
      input_schema: jsonObject(inputSchema, "Input schema"), output_schema: jsonObject(outputSchema, "Output schema"),
      allowed_capabilities: selection.allowed, dependency_lock: { format_version: 1, dependencies: selection.locks },
      permission_boundary: permissions, required_permissions: [...requiredPermissions.values()],
      risk_profile: { ...initial.risk_profile, level: summaryRisk },
      side_effect_profile: { ...initial.side_effect_profile, kind: summaryEffect }, approval_requirement: approval,
    };
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const body = content(new FormData(event.currentTarget));
      setError("");
      const result = await action.send<Version>({
        action: saved ? "capability.update" : "capability.create", kind, identifier: saved?.capability_id,
        version: saved?.number, expected_revision: saved?.revision, name, description, content: body,
      });
      if (result) { setSaved(result); setDirty(false); setValidation(undefined); onSaved(result.capability_id); }
    } catch (error) { setError(errorText(error)); }
  }
  async function validate() {
    if (!saved) return;
    const result = await action.send<{ valid: boolean; errors: string[] }>({ action: "capability.validate", identifier: saved.capability_id, version: saved.number });
    if (result) setValidation(result);
  }
  async function fork() {
    if (!saved) return;
    const result = await action.send<Version>({ action: "capability.fork", identifier: saved.capability_id, version: saved.number });
    if (result) onForked?.(result);
  }
  async function test() {
    try {
      if (!saved || !published || dirty) throw new Error("Save, validate and publish before testing. Drafts are not executable.");
      const input = jsonObject(testInput, "Test input");
      setError("");
      const result = await action.send<{ id: string }>({ action: "execution.run", identifier: saved.capability_id, version: saved.number, input });
      if (result) action.setNotice(`Test admitted as ${result.id}. View Activity for results or required human actions.`);
    } catch (error) { setError(errorText(error)); }
  }
  const selectedSummary = (() => {
    const keys = implementation.kind === "workflow" ? implementation.nodes.flatMap((node) => node.capability ? [optionKey(node.capability)] : []) : allowed;
    return options.items.filter((item) => keys.includes(`${item.definition.id}@${item.version.number}`));
  })();
  return <section className="inline-panel capability-editor">
    <div className="section-toolbar"><div><h3>{kind === "skill" ? t("Skill Editor V1") : t("Workflow Editor V1")}</h3><p>{saved ? t("v{p0} · r{p1} · {p2}", { p0: saved.number, p1: saved.revision, p2: t(versionState(saved)) }) : t("New draft")}</p></div><button type="button" disabled={action.locked} onClick={onClose}>{t("Close editor")}</button></div>
    {published && <p className="warning">{t("Published content is immutable. Create a new draft version to edit it.")}</p>}
    <form className="form-grid" onSubmit={save} onChange={() => { setDirty(true); setValidation(undefined); }}>
      <label>{t("Name")}<input required maxLength={200} disabled={locked} value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label>{t("Description")}<input maxLength={2000} disabled={locked} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <p className="full muted">{t("Do not put credentials in definitions, instructions or mappings. Use connection references. Drafts cannot execute; publishing and testing are separate actions.")}</p>
      {kind === "skill" && <label className="full">{t("Instructions")}<textarea aria-label={t("Instructions")} rows={10} required maxLength={32000} disabled={locked} value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label>}
      <JsonField label={t("Input schema (JSON)")} value={inputSchema} onChange={setInputSchema} disabled={locked} />
      <JsonField label={t("Output schema (JSON)")} value={outputSchema} onChange={setOutputSchema} disabled={locked} />
      {kind === "skill" && <>
        <fieldset className="full capability-picker"><legend>{t("Allowed capabilities (exact published dependencies)")}</legend>
          <p className="muted">{t("Only server-authorized options are selectable. V1 Skills remain tool-free; declaring dependencies does not enable tools.")}</p>
          {options.items.filter((item) => item.definition.id !== saved?.capability_id).map((item) => {
            const key = `${item.definition.id}@${item.version.number}`;
            return <label className="check-label" key={key}><input type="checkbox" disabled={locked} checked={allowed.includes(key)} onChange={(event) => setAllowed(event.target.checked ? [...allowed, key] : allowed.filter((value) => value !== key))} />{item.definition.name}{t(" · v")}{item.version.number} · {t(item.definition.kind)}</label>;
          })}
          {allowed.filter((key) => !options.items.some((item) => `${item.definition.id}@${item.version.number}` === key)).map((key) => <p key={key} className="warning">{t("Unavailable dependency: ")}{key} <button type="button" disabled={locked} onClick={() => setAllowed(allowed.filter((item) => item !== key))}>{t("Remove unavailable dependency")}</button></p>)}
        </fieldset>
        <label>{t("Memory Access")}<input readOnly value={t("Disabled")} aria-describedby="skill-memory-note" /></label>
        <p id="skill-memory-note" className="muted">{t("V1 Skills are isolated and do not inherit Main Agent Memory. Memory cannot be enabled in this editor.")}</p>
      </>}
      <div className="full"><PermissionPicker label={t("Permission boundary")} available={options.permissions} value={permissions} onChange={setPermissions} disabled={locked} /></div>
      <div className="full"><PermissionPicker label={t("Required permissions")} available={permissions} value={required} onChange={setRequired} disabled={locked} /></div>
      <label>{t("Risk summary")}<select aria-label={t("Risk summary")} value={risk} disabled={locked} onChange={(event) => setRisk(event.target.value as typeof risk)}>{["low", "medium", "high", "critical"].map((value) => <option key={value} value={value}>{t(value)}</option>)}</select></label>
      <label>{t("Side-effect summary")}<select aria-label={t("Side-effect summary")} value={effect} disabled={locked} onChange={(event) => setEffect(event.target.value as typeof effect)}>{["none", "local_write", "external_write", "destructive"].map((value) => <option key={value} value={value}>{t(value)}</option>)}</select></label>
      <label>{t("Approval requirement")}<select aria-label={t("Approval requirement")} value={approval} disabled={locked} onChange={(event) => setApproval(event.target.value as typeof approval)}><option value="platform_minimum">{t("Platform minimum (cannot disable safety)")}</option><option value="always">{t("Always require approval")}</option></select></label>
      <div className="full"><RiskSummary risk={risk} effect={effect} approval={approval} />
        {selectedSummary.map((item) => <p key={`${item.definition.id}@${item.version.number}`} className="muted">{item.definition.name}{t(" v")}{item.version.number}: {t(item.version.content.risk_profile.level)} / {t(item.version.content.side_effect_profile.kind)} / {item.version.content.required_permissions.map((permission) => permission.action).join(", ") || t("no required permissions")}</p>)}
        <p className="muted">{t("Workflow dependency and notification risks raise the saved summary automatically. Server publish validation is authoritative.")}</p>
      </div>
      {implementation.kind === "workflow" && <WorkflowDesigner graph={implementation} onChange={(graph) => { setImplementation(graph); setDirty(true); }} options={{ ...options, items: options.items.filter((item) => item.definition.id !== saved?.capability_id) }} boundary={permissions} disabled={locked} />}
      <div className="button-row full">
        {!published && <button className="primary" disabled={action.locked || !dirty}>{t("Save Draft")}</button>}
        <button type="button" disabled={action.locked || !saved || dirty} onClick={validate}>{t("Validate saved version")}</button>
        {!published && <button type="button" disabled={action.locked || !saved || dirty || !validation?.valid} onClick={() => saved && action.send({ action: "capability.publish", identifier: saved.capability_id, version: saved.number, expected_revision: saved.revision }, () => onSaved(saved.capability_id))}>{t("Review Publish")}</button>}
        {published && <button type="button" disabled={action.locked} onClick={fork}>{t("Create new draft version")}</button>}
      </div>
    </form>
    {dirty && <p className="muted">{t("Unsaved changes. Save before validating or publishing.")}</p>}
    {validation && <div className={validation.valid ? "feedback" : "feedback error"} role="status">{validation.valid ? t("Server validation passed.") : <div><strong>{t("Validation errors")}</strong><ul>{validation.errors.map((message, index) => <li key={index}>{t(message)}</li>)}</ul></div>}</div>}
    <Feedback error={error} /><CommandFeedback action={action} />
    <details className="separated"><summary>{t("Test published version")}</summary><p className="muted">{t("Tests use the same InvocationService and durable Execution as the CLI. Model-backed Skills may call your configured paid provider; safety gates still apply.")}</p>
      <JsonField label={t("Test input (JSON)")} value={testInput} onChange={setTestInput} disabled={action.locked} />
      <button type="button" disabled={action.locked || !published || dirty} onClick={test}>{t("Run published test")}</button>
    </details>
  </section>;
}
