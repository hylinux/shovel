"use client";

import { useI18n } from "./locale-context";

import { useState } from "react";
import { api, errorText, segment, useQuery, type Items, type Status } from "./api";
import { CommandFeedback, JsonField, RiskSummary, usePlatformAction } from "./platform";
import { isPlatformRun, type Approval, type Execution, type InputRequest, type Interaction } from "./platform-types";
import { forgetPendingRequest, pendingRequests, type PendingRequest } from "./request-recovery";
import { Badge, Empty, Feedback } from "./ui";

export function ExecutionActivity({ active = true, serviceId, onClose }: { active?: boolean; serviceId?: string; onClose?: () => void }) {
  const { t, date } = useI18n();
  const path = serviceId ? `/platform/proactive/${segment(serviceId)}/history` : "/platform/executions";
  const query = useQuery<Items<Execution>>(active ? path : null, 2000);
  const approvals = useQuery<Items<Approval>>(active ? "/platform/approvals" : null, 2000);
  const [selected, setSelected] = useState("");
  return <section className="platform-section">
    <div className="section-toolbar"><div><h2>{serviceId ? t("Service history") : t("Execution activity")}</h2><p>{t("Durable Execution facts shared with the CLI. Skipped events are not durable history. Latest 100 entries.")}</p></div>{onClose && <button onClick={onClose}>{t("Back to services")}</button>}</div>
    {!serviceId && <PlatformRecovery refresh={query.refresh} />}
    <Feedback error={query.error || approvals.error} retry={() => { query.refresh(); approvals.refresh(); }} />
    {query.loading && <p role="status">{t("Loading activity...")}</p>}
    {query.data?.items.length === 0 && <Empty title={t("No executions yet")}>{t("Run a published capability or an enabled service.")}</Empty>}
    {query.data?.items.map((execution) => {
      const snapshot = execution.snapshot, service = snapshot.context.proactive_snapshot;
      const approval = approvals.data?.items.find((row) => row.execution_id === execution.id);
      return <article className="operation-row" key={execution.id}>
        <div className="title-line"><h3>{snapshot.capability.capability_id}{t(" · v")}{snapshot.capability.number}</h3><Badge state={execution.wait_reason || execution.state} /></div>
        <dl className="platform-facts">
          <div><dt>{t("Invocation source")}</dt><dd>{t(snapshot.context.source)}</dd></div>
          <div><dt>{t("Service revision")}</dt><dd>{service ? `${service.name || service.id} · r${service.revision}` : t("None")}</dd></div>
          <div><dt>{t("Decision")}</dt><dd>{t(execution.wait_reason || execution.error || (execution.state === "succeeded" ? "Completed" : execution.state))}</dd></div>
          <div><dt>{t("Approval")}</dt><dd>{approval ? t(approval.state) : execution.approval_request_id || t("No request")}</dd></div>
          <div><dt>{t("Duration")}</dt><dd>{execution.duration_seconds === null ? t("Not started") : `${execution.duration_seconds.toFixed(1)} s`}</dd></div>
          <div><dt>{t("Created")}</dt><dd>{date(execution.created_at)}</dd></div>
        </dl>
        <p className="path">{execution.id}</p>
        {execution.error && <p className="reason">{execution.error}</p>}
        <details><summary>{t("Result")}</summary><pre>{JSON.stringify(execution.output, null, 2)}</pre></details>
        <button aria-expanded={selected === execution.id} onClick={() => setSelected(selected === execution.id ? "" : execution.id)}>{t("Execution details")}</button>
        {selected === execution.id && <ExecutionDetails id={execution.id} refresh={query.refresh} />}
      </article>;
    })}
  </section>;
}

export function ExecutionDetails({ id, refresh }: { id: string; refresh: () => void }) {
  const { t } = useI18n();
  const query = useQuery<Execution>(`/platform/executions/${segment(id)}`, 2000);
  const action = usePlatformAction();
  const execution = query.data;
  return <div className="inline-panel">
    <Feedback error={query.error} retry={query.refresh} />
    {execution && <>
      <RiskSummary risk={execution.snapshot.capability.content.risk_profile.level} effect={execution.snapshot.capability.content.side_effect_profile.kind} approval={execution.snapshot.capability.content.approval_requirement} />
      <details><summary>{t("Frozen version, permissions, checkpoint and attempt history")}</summary><pre tabIndex={0}>{JSON.stringify(execution, null, 2)}</pre></details>
      <div className="button-row">
        {!["succeeded", "failed", "cancelled"].includes(execution.state) && <button disabled={action.locked} onClick={() => action.send({ action: "execution.cancel", identifier: id }, () => { query.refresh(); refresh(); })}>{t("Cancel execution")}</button>}
        {execution.state === "suspended" && <button disabled={action.locked} onClick={() => action.send({ action: "execution.resume", identifier: id }, () => { query.refresh(); refresh(); })}>{t("Request resume (server gates apply)")}</button>}
      </div>
    </>}
    <CommandFeedback action={action} />
  </div>;
}

function PlatformRecovery({ refresh }: { refresh: () => void }) {
  const { t } = useI18n();
  const [rows, setRows] = useState<PendingRequest[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    try {
      const status = await api<Status>("/status");
      const found = pendingRequests(status.workspace_id).filter((row) => row.path === "/platform/commands");
      setRows(found); setError(""); setNotice(found.length ? "" : "No interrupted Execution requests.");
    } catch (error) { setError(errorText(error)); }
  }
  async function recover(request: PendingRequest, resend: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
      const status = await api<Status>("/status");
      if (request.workspace !== status.workspace_id) throw new Error("This receipt belongs to another workspace.");
      const command: unknown = JSON.parse(request.body || "");
      if (!isPlatformRun(command)) throw new Error("Invalid Execution recovery command.");
      const suffix = command.action === "proactive.run" ? `?service_id=${segment(command.identifier || "")}` : "";
      const found = await api<{ execution: Execution | null }>(`/platform/requests/${segment(request.key)}${suffix}`);
      let execution = found.execution;
      if (!execution && resend) execution = await api<Execution>(request.path, {
        method: request.method, body: request.body, headers: { "Idempotency-Key": request.key },
      });
      if (!execution) { setNotice("No committed Execution found. Resend only the original request with its preserved key."); return; }
      forgetPendingRequest(request.key);
      setRows((current) => current.filter((row) => row.key !== request.key));
      setNotice(`Recovered ${execution.id}: ${execution.state}.`); refresh();
    } catch (error) { setError(errorText(error)); }
    finally { setBusy(false); }
  }
  return <details className="inline-panel"><summary>{t("Interrupted Execution requests")}</summary>
    <p>{t("Inspect persisted state before resending. The original workspace-scoped key prevents duplicate admission; this is not Exactly Once delivery.")}</p>
    <button disabled={busy} onClick={load}>{t("Find interrupted Execution requests")}</button>
    {rows.map((request) => <article key={request.key}><p className="path">{t("Request key: ")}{request.key}</p><div className="button-row"><button disabled={busy} onClick={() => recover(request, false)}>{t("Find committed Execution")}</button><button disabled={busy} onClick={() => recover(request, true)}>{t("Resend original request with same key")}</button></div></article>)}
    <Feedback error={error} notice={notice} />
  </details>;
}

export function ApprovalCenter({ active }: { active: boolean }) {
  const { t } = useI18n();
  const query = useQuery<Items<Approval>>(active ? "/platform/approvals" : null, 2000);
  const interactions = useQuery<Items<Interaction>>(active ? "/platform/interactions" : null, 2000);
  const [details, setDetails] = useState("");
  const refresh = () => { query.refresh(); interactions.refresh(); };
  return <section className="platform-section">
    <div className="section-toolbar"><div><h2>{t("Approval requests")}</h2><p>{t("Approval confirms an already authorized action. It never grants permissions. Latest 100 requests.")}</p></div><button onClick={refresh}>{t("Refresh")}</button></div>
    <Feedback error={query.error || interactions.error} retry={refresh} />
    {query.loading && <p role="status">{t("Loading approval requests...")}</p>}
    {query.data?.items.length === 0 && <Empty title={t("No approval requests")}>{t("Requests from manual runs, services and workflows appear here.")}</Empty>}
    {query.data?.items.map((request) => <ApprovalCard key={request.id} request={request} details={details === request.id} onDetails={() => setDetails(details === request.id ? "" : request.id)} refresh={refresh} />)}
    <h2 className="separated">{t("Input requests and notifications")}</h2>
    {interactions.data?.items.filter((row) => row.kind !== "approval").map((row) => <InteractionCard key={row.id} interaction={row} refresh={refresh} />)}
  </section>;
}

function ApprovalCard({ request, details, onDetails, refresh }: { request: Approval; details: boolean; onDetails: () => void; refresh: () => void }) {
  const { t, date } = useI18n();
  const action = usePlatformAction();
  const pending = request.state === "pending";
  return <article className="operation-row">
    <div className="title-line"><h3>{t(request.capability.kind)} · {request.capability.capability_id}{t(" v")}{request.capability.number}</h3><Badge state={request.state} /></div>
    <p>{t("Requested action: ")}{request.side_effect.description || t("{p0} execution / explicit workflow approval", { p0: t(request.capability.kind) })}</p>
    <p className="muted">{t("Service: ")}{request.service?.name || request.service?.id || t("None")}{t(" · Workflow: ")}{request.capability.kind === "workflow" ? request.capability.capability_id : t("None")}{t(" · Expires ")}{date(request.expires_at)}</p>
    <RiskSummary risk={request.risk.level} effect={request.side_effect.kind} approval="required" />
    <p>{t("Reason: ")}{request.reasons.join("; ")}</p><p className="path">{t("Approval ")}{request.id}{t(" · Execution ")}{request.execution_id}{t(" · User ")}{request.user_id}</p>
    <div className="button-row"><button className="primary" disabled={!pending || action.locked} onClick={() => action.send({ action: "approval.approve", identifier: request.id, execution_id: request.execution_id }, refresh)}>{t("Review approval")}</button>
      <button disabled={!pending || action.locked} onClick={() => action.send({ action: "approval.reject", identifier: request.id, execution_id: request.execution_id }, refresh)}>{t("Review rejection")}</button>
      <button aria-expanded={details} onClick={onDetails}>{t("View details")}</button></div>
    <CommandFeedback action={action} />
    {details && <ExecutionDetails id={request.execution_id} refresh={refresh} />}
  </article>;
}

function InteractionCard({ interaction, refresh }: { interaction: Interaction; refresh: () => void }) {
  const { t, date } = useI18n();
  const [open, setOpen] = useState(false);
  return <article className="operation-row"><div className="title-line"><h3>{t(interaction.kind.replaceAll("_", " "))}</h3><Badge state={interaction.state} /></div>
    <p>{interaction.message}</p><p className="muted">{date(interaction.created_at)}</p>
    <p className="path">{t("Interaction ")}{interaction.id}{t(" · Execution ")}{interaction.execution_id}</p>
    {interaction.kind === "input_request" && interaction.state === "open" && <button aria-expanded={open} onClick={() => setOpen(!open)}>{t("Provide structured input")}</button>}
    {open && <InputResponse interaction={interaction} refresh={refresh} />}
  </article>;
}

function InputResponse({ interaction, refresh }: { interaction: Interaction; refresh: () => void }) {
  const { t, date } = useI18n();
  const query = useQuery<InputRequest>(`/platform/interactions/${segment(interaction.source_id)}?execution_id=${segment(interaction.execution_id)}`);
  const action = usePlatformAction();
  const [response, setResponse] = useState("{}");
  const [error, setError] = useState("");
  async function submit() {
    try {
      const value = JSON.parse(response);
      setError("");
      await action.send({ action: "interaction.input", identifier: interaction.source_id, execution_id: interaction.execution_id, response: value }, refresh);
    } catch (error) { setError(errorText(error)); }
  }
  return <div className="inline-panel">
    <Feedback error={query.error || error} retry={query.refresh} />
    {query.data && <><p>{t("Bound to request ")}{query.data.id}{t(", execution ")}{query.data.execution_id}{t(", user ")}{query.data.user_id}{t(". Expires ")}{date(query.data.expires_at)}.</p>
      <details><summary>{t("Required response schema")}</summary><pre>{JSON.stringify(query.data.response_schema, null, 2)}</pre></details>
      <JsonField label={t("Input response (JSON)")} value={response} onChange={setResponse} disabled={action.locked} />
      <button disabled={action.locked || query.data.state !== "pending"} onClick={submit}>{t("Review input response")}</button></>}
    <CommandFeedback action={action} />
  </div>;
}
