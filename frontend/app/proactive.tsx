"use client";

import { useState, type FormEvent } from "react";
import { date, errorText, useQuery, type Items } from "./api";
import { ExecutionActivity } from "./platform-activity";
import { CommandFeedback, JsonField, PermissionPicker, usePlatformAction } from "./platform";
import { approvalPreferences, bindingMap, conditionList, jsonObject, observedService, optionKey, type AutomationControl, type PlatformOptions, type ProactiveConfiguration, type ProactiveService, type Trigger } from "./platform-types";
import { Badge, Empty, Feedback } from "./ui";

export function ProactiveServices({ active }: { active: boolean }) {
  const query = useQuery<Items<ProactiveService>>(active ? "/platform/proactive" : null, 3000);
  const options = useQuery<PlatformOptions>(active ? "/platform/options" : null);
  const [editor, setEditor] = useState<ProactiveService | "new">();
  const [history, setHistory] = useState("");
  function saved() { setEditor(undefined); query.refresh(); options.refresh(); }
  return <section className="platform-section">
    <GlobalAutomation active={active} refresh={query.refresh} />
    <Feedback error={query.error || options.error} retry={() => { query.refresh(); options.refresh(); }} />
    {history ? <ExecutionActivity active={active} serviceId={history} onClose={() => setHistory("")} /> : <>
      <div className="section-toolbar"><div><h2>Proactive services</h2><p>Triggers and conditions invoke published capabilities. All execution and safety decisions stay on the server.</p></div><button className="primary" disabled={!options.data} onClick={() => setEditor("new")}>New service draft</button></div>
      {editor && options.data && <ProactiveEditor key={editor === "new" ? "new" : editor.id} initial={editor === "new" ? undefined : editor} options={options.data} onSaved={saved} onClose={() => setEditor(undefined)} />}
      {query.loading && <p role="status">Loading services...</p>}
      {query.data?.items.length === 0 && <Empty title="No proactive services">Save a disabled draft, review its target and explicitly enable it.</Empty>}
      {query.data?.items.map((service) => <ServiceCard key={service.id} service={service} refresh={query.refresh} onEdit={() => { setEditor(service); options.refresh(); }} onHistory={() => setHistory(service.id)} />)}
    </>}
  </section>;
}

function GlobalAutomation({ active, refresh }: { active: boolean; refresh: () => void }) {
  const query = useQuery<AutomationControl>(active ? "/platform/automation" : null, 3000);
  const action = usePlatformAction();
  const control = query.data;
  return <section className="inline-panel automation-control" aria-label="Global automation control">
    <div className="section-toolbar"><div><h2>Automation {control ? control.paused ? "paused" : "active" : "status"}</h2><p>Global pause blocks automatic Event/Schedule admission only. Existing executions and manual runs continue. Individual service states are preserved; resume does not replay skipped events.</p></div>
      <button disabled={!control || action.locked} onClick={() => control && action.send({ action: control.paused ? "automation.resume" : "automation.pause", expected_revision: control.revision }, () => { query.refresh(); refresh(); })}>{control?.paused ? "Review global resume" : "Pause automation"}</button></div>
    <Feedback error={query.error} retry={query.refresh} /><CommandFeedback action={action} />
  </section>;
}
function triggerText(trigger: Trigger) {
  if (trigger.kind === "manual") return "Manual run";
  if (trigger.kind === "event") return `Event: ${trigger.event_type}${trigger.source ? ` (${trigger.source})` : ""}`;
  const schedule = trigger.schedule;
  return schedule.kind === "interval" ? `Every ${schedule.interval_seconds} seconds (${schedule.timezone})` : `Daily ${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")} (${schedule.timezone})`;
}
function ServiceCard({ service: incoming, refresh, onEdit, onHistory }: { service: ProactiveService; refresh: () => void; onEdit: () => void; onHistory: () => void }) {
  const action = usePlatformAction();
  const service = observedService(incoming, action.serviceStatus);
  const [payload, setPayload] = useState("{}");
  const [runOpen, setRunOpen] = useState(false);
  const [error, setError] = useState("");
  async function run() {
    try {
      const input = jsonObject(payload, "Manual payload");
      setError("");
      const execution = await action.send<{ id: string }>({ action: "proactive.run", identifier: service.id, input }, refresh);
      if (execution) action.setNotice(`Execution ${execution.id} admitted. View History for persisted status.`);
    } catch (error) { setError(errorText(error)); }
  }
  return <article className="operation-row">
    <div className="title-line"><h3>{service.name || service.id}</h3><Badge state={!service.enabled ? "disabled" : service.paused ? "paused" : "enabled"} /><span className="muted">r{service.revision}</span></div>
    <p className="path">{service.id}</p>
    {service.description && <p>{service.description}</p>}
    <dl className="platform-facts">
      <div><dt>Trigger</dt><dd>{triggerText(service.configuration.trigger)}</dd></div>
      <div><dt>Target capability</dt><dd>{service.target_name || service.configuration.target.capability_id} · {service.target_kind}</dd></div>
      <div><dt>Capability version</dt><dd>{service.configuration.target.version_policy === "exact" ? `Exact v${service.configuration.target.version}` : `Latest published (currently ${service.resolved_version ? `v${service.resolved_version}` : "unavailable"})`}</dd></div>
      <div><dt>Approval preference</dt><dd>{approvalPreferences.find((item) => item.value === service.configuration.approval_preference)?.label}</dd></div>
      <div><dt>Notification policy</dt><dd>{service.configuration.notification_policy}</dd></div>
      <div><dt>Last activity</dt><dd>{date(service.last_activity)}</dd></div>
      <div><dt>Next trigger</dt><dd>{date(service.next_trigger)}</dd></div>
    </dl>
    {service.target_error && <Feedback error={service.target_error} />}
    <div className="button-row">
      <button disabled={action.locked || !service.enabled || service.paused} aria-expanded={runOpen} onClick={() => setRunOpen(!runOpen)}>Run Now</button>
      {service.enabled && !service.paused && <button disabled={action.locked} onClick={() => action.send({ action: "proactive.pause", identifier: service.id, expected_revision: service.revision }, refresh)}>Pause</button>}
      {service.enabled && service.paused && <button disabled={action.locked} onClick={() => action.send({ action: "proactive.resume", identifier: service.id, expected_revision: service.revision }, refresh)}>Review resume</button>}
      <button disabled={action.locked} onClick={() => action.send({ action: service.enabled ? "proactive.disable" : "proactive.enable", identifier: service.id, expected_revision: service.revision }, refresh)}>{service.enabled ? "Disable" : "Review enable"}</button>
      <button disabled={action.locked} onClick={onEdit}>Edit</button><button onClick={onHistory}>History</button>
    </div>
    {runOpen && <div className="inline-panel"><JsonField label="Manual event payload (JSON)" value={payload} onChange={setPayload} disabled={action.locked} help="Conditions and mappings still apply; event fields are available under payload." /><button disabled={action.locked} onClick={run}>Run with this payload</button></div>}
    <Feedback error={error} /><CommandFeedback action={action} />
  </article>;
}

function ProactiveEditor({ initial, options, onSaved, onClose }: { initial?: ProactiveService; options: PlatformOptions; onSaved: () => void; onClose: () => void }) {
  const targets = options.items.filter((item) => item.definition.kind === "skill" || item.definition.kind === "workflow");
  const first = targets[0];
  const config = initial?.configuration;
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [trigger, setTrigger] = useState<Trigger>(config?.trigger || { kind: "manual" });
  const [target, setTarget] = useState(config ? `${config.target.capability_id}@${config.target.version || targets.find((item) => item.definition.id === config.target.capability_id)?.version.number || 1}` : first ? `${first.definition.id}@${first.version.number}` : "");
  const [policy, setPolicy] = useState<"exact" | "latest_published">(config?.target.version_policy || "exact");
  const [mapping, setMapping] = useState(JSON.stringify(config?.input_mapping || {}, null, 2));
  const [conditions, setConditions] = useState(JSON.stringify(config?.conditions || [], null, 2));
  const [permissions, setPermissions] = useState(config?.permission_boundary || []);
  const [approval, setApproval] = useState(config?.approval_preference || "MINIMUM_REQUIRED");
  const [notification, setNotification] = useState(config?.notification_policy || "none");
  const [quiet, setQuiet] = useState(config?.quiet_hours || null);
  const [cooldown, setCooldown] = useState(config?.cooldown_seconds || 0);
  const [error, setError] = useState("");
  const action = usePlatformAction();
  async function save(event: FormEvent) {
    event.preventDefault();
    try {
      const selected = targets.find((item) => optionKey({ capability_id: item.definition.id, version: item.version.number }) === target);
      if (!selected) throw new Error("Choose a currently available Skill or Workflow.");
      const configuration: ProactiveConfiguration = {
        trigger, target: { capability_id: selected.definition.id, version_policy: policy, version: policy === "exact" ? selected.version.number : null },
        input_mapping: bindingMap(mapping, "Input mapping"), conditions: conditionList(conditions),
        permission_boundary: permissions, approval_preference: approval, notification_policy: notification, quiet_hours: quiet, cooldown_seconds: cooldown,
      };
      setError("");
      await action.send({
        action: initial ? "proactive.update" : "proactive.create", identifier: initial?.id,
        expected_revision: initial?.revision, name, description, configuration,
      }, onSaved);
    } catch (error) { setError(errorText(error)); }
  }
  return <form className="inline-panel form-grid" onSubmit={save}>
    <h3 className="full">{initial ? `Edit service r${initial.revision}` : "New service draft"}</h3>
    <p className="full muted">Saving disables the new revision. Enable it only after reviewing the server preview. Do not enter credentials; use connection references.</p>
    <label>Name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} disabled={action.locked} /></label>
    <label>Description<input maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} disabled={action.locked} /></label>
    <TriggerEditor value={trigger} onChange={setTrigger} disabled={action.locked} />
    <label>Target Skill / Workflow<select aria-label="Target Skill / Workflow" required value={target} onChange={(event) => setTarget(event.target.value)} disabled={action.locked}><option value="">Select a published target</option>{!targets.some((item) => `${item.definition.id}@${item.version.number}` === target) && target && <option value={target}>Unavailable: {target}</option>}{targets.map((item) => <option key={`${item.definition.id}@${item.version.number}`} value={`${item.definition.id}@${item.version.number}`}>{item.definition.name} · v{item.version.number} · {item.definition.kind}</option>)}</select></label>
    <label>Version policy<select aria-label="Version policy" value={policy} onChange={(event) => setPolicy(event.target.value as typeof policy)} disabled={action.locked}><option value="exact">Exact version (recommended)</option><option value="latest_published">Latest published at invocation</option></select></label>
    <JsonField label="Conditions (JSON array)" value={conditions} onChange={setConditions} disabled={action.locked} help={'Operators: eq, ne, contains, in, exists, gt, gte, lt, lte. Example: [{"path":["payload","count"],"operator":"gte","value":1}]'} />
    <JsonField label="Input mapping (JSON)" value={mapping} onChange={setMapping} disabled={action.locked} help={'Example: {"text":{"source":"event","path":["payload","text"]}}. Literal values use {"source":"literal","value":...}.'} />
    <div className="full"><PermissionPicker label="Permission boundary" available={options.permissions} value={permissions} onChange={setPermissions} disabled={action.locked} /></div>
    <label>Approval preference<select aria-label="Approval preference" value={approval} onChange={(event) => setApproval(event.target.value as typeof approval)} disabled={action.locked}>{approvalPreferences.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
    <label>Notification preference<select aria-label="Notification preference" value={notification} onChange={(event) => setNotification(event.target.value as typeof notification)} disabled={action.locked}><option value="none">None</option><option value="failures">Failures</option><option value="all">All activity</option></select></label>
    <label>Cooldown (seconds)<input type="number" min={0} max={31536000} required value={cooldown} onChange={(event) => setCooldown(Number(event.target.value))} disabled={action.locked} /></label>
    <label className="check-label"><input type="checkbox" checked={Boolean(quiet)} disabled={action.locked} onChange={(event) => setQuiet(event.target.checked ? { timezone: "UTC", start_minute: 1380, end_minute: 420 } : null)} />Quiet hours</label>
    {quiet && <><label>Quiet hours timezone<input required value={quiet.timezone} onChange={(event) => setQuiet({ ...quiet, timezone: event.target.value })} disabled={action.locked} /></label><label>Quiet hours start<input required type="time" value={time(quiet.start_minute)} onChange={(event) => setQuiet({ ...quiet, start_minute: minutes(event.target.value) })} disabled={action.locked} /></label><label>Quiet hours end<input required type="time" value={time(quiet.end_minute)} onChange={(event) => setQuiet({ ...quiet, end_minute: minutes(event.target.value) })} disabled={action.locked} /></label></>}
    <div className="button-row full"><button className="primary" disabled={action.locked || !targets.length}>Save disabled draft</button><button type="button" disabled={action.locked} onClick={onClose}>Close editor</button></div>
    <div className="full"><Feedback error={error} /><CommandFeedback action={action} /></div>
  </form>;
}
function time(value: number) { return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`; }
function minutes(value: string) { const [hour, minute] = value.split(":").map(Number); return hour * 60 + minute; }
function TriggerEditor({ value, onChange, disabled }: { value: Trigger; onChange: (trigger: Trigger) => void; disabled: boolean }) {
  function changeKind(kind: Trigger["kind"]) {
    onChange(kind === "manual" ? { kind } : kind === "event" ? { kind, event_type: "", source: null } : {
      kind, schedule: { kind: "interval", interval_seconds: 3600, timezone: "UTC", anchor_at: 0, hour: null, minute: null, missed_policy: "skip", misfire_grace_seconds: 60 },
    });
  }
  return <>
    <label>Trigger<select aria-label="Trigger" value={value.kind} onChange={(event) => changeKind(event.target.value as Trigger["kind"])} disabled={disabled}><option value="manual">Manual run</option><option value="event">Event</option><option value="schedule">Schedule</option></select></label>
    {value.kind === "event" && <><label>Event type<input required value={value.event_type} onChange={(event) => onChange({ ...value, event_type: event.target.value })} disabled={disabled} /></label><label>Event source (optional)<input value={value.source || ""} onChange={(event) => onChange({ ...value, source: event.target.value || null })} disabled={disabled} /></label></>}
    {value.kind === "schedule" && <>
      <label>Schedule kind<select aria-label="Schedule kind" value={value.schedule.kind} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, kind: event.target.value as "interval" | "daily", interval_seconds: event.target.value === "interval" ? 3600 : null, hour: event.target.value === "daily" ? 9 : null, minute: event.target.value === "daily" ? 0 : null } })} disabled={disabled}><option value="interval">Interval</option><option value="daily">Daily</option></select></label>
      <label>IANA timezone<input required value={value.schedule.timezone} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, timezone: event.target.value } })} disabled={disabled} /></label>
      {value.schedule.kind === "interval" ? <><label>Interval seconds<input required type="number" min={1} max={31536000} value={value.schedule.interval_seconds || ""} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, interval_seconds: Number(event.target.value) } })} disabled={disabled} /></label><label>Anchor (UTC Unix seconds)<input required type="number" min={0} value={value.schedule.anchor_at} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, anchor_at: Number(event.target.value) } })} disabled={disabled} /></label></> : <label>Local time<input required type="time" value={time((value.schedule.hour || 0) * 60 + (value.schedule.minute || 0))} onChange={(event) => { const minute = minutes(event.target.value); onChange({ ...value, schedule: { ...value.schedule, hour: Math.floor(minute / 60), minute: minute % 60 } }); }} disabled={disabled} /></label>}
      <label>Missed occurrence policy<select aria-label="Missed occurrence policy" value={value.schedule.missed_policy} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, missed_policy: event.target.value as "skip" | "fire_once" } })} disabled={disabled}><option value="skip">Skip missed occurrences</option><option value="fire_once">Fire latest occurrence once</option></select></label>
      <label>Misfire grace (seconds)<input required type="number" min={0} max={86400} value={value.schedule.misfire_grace_seconds} onChange={(event) => onChange({ ...value, schedule: { ...value.schedule, misfire_grace_seconds: Number(event.target.value) } })} disabled={disabled} /></label>
      <p className="full muted">DST gaps are skipped; repeated local times use their first occurrence. This is a business schedule, not an Execution Snooze timer.</p>
    </>}
  </>;
}
