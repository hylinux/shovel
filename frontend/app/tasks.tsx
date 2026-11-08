"use client";

import { useI18n } from "./locale-context";

import { useState } from "react";
import { active, api, errorText, segment, useAction, useQuery, type Items, type Job, type JobEvent, type Operation, type Resource, type Status } from "./api";
import { usePageQuery } from "./pagination-query";
import { forgetPendingRequest, pendingRequests, type PendingRequest } from "./request-recovery";
import { Badge, Empty, Feedback, Pagination } from "./ui";

export function Tasks({ resources }: { resources: Resource[] }) {
  const { t } = useI18n();
  const jobs = usePageQuery<Job>("/jobs", 2000);
  const operations = usePageQuery<Operation>("/operations", 2000);
  const action = useAction();
  const [cleanup, setCleanup] = useState(false);
  const [resourceId, setResourceId] = useState("");
  async function clean() {
    const result = await action.run<Operation>("/index/cleanup", resourceId ? { resource_id: resourceId } : {});
    if (result) { action.setNotice("Index cleanup queued."); setCleanup(false); operations.refresh(); }
  }
  return <section>
    <RecoveryRequests refresh={operations.refresh} />
    <HistoryMaintenance refresh={() => { operations.refresh(); jobs.refresh(); }} />
    <div className="section-toolbar"><div><h2>{t("Scan jobs")}</h2><p>{t("Progress updates every two seconds while this view is open.")}</p></div><button onClick={() => setCleanup(!cleanup)} aria-expanded={cleanup}>{t("Clean up index…")}</button></div>
    {cleanup && <div className="inline-panel"><h3>{t("Clean up index")}</h3><p>{t("Run backend index maintenance for one resource or the whole index. Original files are not changed.")}</p><label>{t("Scope")}<select value={resourceId} onChange={(event) => setResourceId(event.target.value)}><option value="">{t("All resources")}</option>{resources.map((resource) => <option key={resource.id} value={resource.id}>{resource.name}</option>)}</select></label><div className="button-row"><button disabled={action.busy} onClick={clean}>{action.busy ? t("Queuing…") : t("Confirm cleanup")}</button><button onClick={() => setCleanup(false)} disabled={action.busy}>{t("Cancel")}</button></div></div>}
    <Feedback error={action.error} notice={action.notice} />
    <Feedback error={jobs.error} retry={jobs.refresh} />
    {jobs.loading ? <p role="status">{t("Loading scan jobs…")}</p> : jobs.data?.items.length === 0 ? <Empty title={t("No scans yet")}>{t("Start a scan from Resources. Its stages and document events will appear here.")}</Empty> : jobs.data?.items.map((job) => <JobRow key={job.id} job={job} refresh={jobs.refresh} />)}
    {jobs.data && <Pagination label={t("Scan jobs")} pagination={jobs} onPage={jobs.setPage} onPageSize={jobs.setPageSize} />}
    <div className="section-toolbar separated"><div><h2>{t("Operations")}</h2><p>{t("Resource changes and index maintenance, including failed requests.")}</p></div></div>
    <Feedback error={operations.error} retry={operations.refresh} />
    {operations.loading ? <p role="status">{t("Loading operations…")}</p> : operations.data?.items.length === 0 ? <Empty title={t("No operations yet")}>{t("Resource actions will be recorded here.")}</Empty> : <div className="operation-list">{operations.data?.items.map((operation) => <OperationRow key={operation.id} operation={operation} refresh={operations.refresh} />)}</div>}
    {operations.data && <Pagination label={t("Operations")} pagination={operations} onPage={operations.setPage} onPageSize={operations.setPageSize} />}
  </section>;
}

function HistoryMaintenance({ refresh }: { refresh: () => void }) {
  const { t } = useI18n();
  const action = useAction();
  const [confirm, setConfirm] = useState(false);
  const [days, setDays] = useState(30);
  async function prune() {
    const receipt = await action.run<Operation>("/history/prune", { keep_days: days, limit: 1000 });
    if (receipt) { setConfirm(false); action.setNotice("History maintenance queued. Idempotency receipts and active work are retained."); refresh(); }
  }
  return <details className="inline-panel"><summary>{t("Task history retention")}</summary>
    <p>{t("Delete old terminal jobs, their events, retrieval logs, completed cleanup records and unkeyed operations. Sessions, documents, pending cleanup and keyed request receipts are retained. Nothing is deleted automatically.")}</p>
    <label>{t("Keep latest days")}<input type="number" min={1} max={36500} required value={days} onChange={(event) => setDays(Number(event.target.value))} /></label>
    <button disabled={action.busy || !Number.isInteger(days) || days < 1 || days > 36500} onClick={() => setConfirm(true)}>{t("Prune old history…")}</button>
    {confirm && <div role="alert" className="confirmation"><p>{t("Delete up to 1000 records per category older than ")}{days}{t(" days? This cannot be undone.")}</p>
      <button disabled={action.busy} onClick={prune}>{t("Confirm history deletion")}</button><button onClick={() => setConfirm(false)}>{t("Cancel")}</button></div>}
    <Feedback error={action.error} notice={action.notice} /></details>;
}

function RecoveryRequests({ refresh }: { refresh: () => void }) {
  const { t } = useI18n();
  const [requests, setRequests] = useState<PendingRequest[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    try {
      const status = await api<Status>("/status");
      const rows = pendingRequests(status.workspace_id).filter((row) => row.path !== "/platform/commands");
      setRequests(rows); setError(""); setNotice(rows.length ? "" : "No interrupted browser requests found.");
    } catch (error) { setError(errorText(error)); }
  }
  async function recover(request: PendingRequest, resend: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
      const status = await api<Status>("/status");
      if (status.workspace_id !== request.workspace) throw new Error("This receipt belongs to another workspace.");
      const receipt = resend
        ? await api<Operation>(request.path, {
          method: request.method, body: request.body, headers: { "Idempotency-Key": request.key },
        })
        : await api<Operation>(`/operations/${segment(request.id)}`);
      forgetPendingRequest(request.key);
      setRequests((rows) => rows.filter((row) => row.key !== request.key));
      setNotice(`Recovered ${receipt.id}: ${receipt.state}. Its receipt is listed below.`);
      refresh();
    } catch (error) { setError(errorText(error)); }
    finally { setBusy(false); }
  }
  return <div className="inline-panel">
    <h3>{t("Interrupted requests")}</h3>
    <p>{t("A lost response does not mean an operation failed. Find its receipt first, or resend the original request with the same key.")}</p>
    <button disabled={busy} onClick={load}>{t("Find interrupted requests")}</button>
    <Feedback error={error} notice={notice} />
    {requests.map((request) => <article key={request.key}>
      <p className="path">{request.id} · {request.method} {request.path}</p>
      <div className="button-row"><button disabled={busy} onClick={() => recover(request, false)}>{t("Find receipt")}</button>
        <button disabled={busy} onClick={() => recover(request, true)}>{t("Resend same request")}</button></div>
    </article>)}
  </div>;
}

function JobRow({ job, refresh }: { job: Job; refresh: () => void }) {
  const { t, date } = useI18n();
  const [eventsOpen, setEventsOpen] = useState(false);
  const action = useAction();
  const progress = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
  const stage = job.state === "queued" ? "Waiting for worker"
    : job.state === "running" ? job.current_stage || "Running"
    : ["failed", "cancelled"].includes(job.state) ? "Stopped"
    : ["succeeded", "completed", "completed_with_errors"].includes(job.state) ? "Finished"
    : "No active stage";
  async function cancel() {
    const result = await action.run<Job>(`/jobs/${segment(job.id)}/cancel`, {});
    if (result) { action.setNotice("Cancellation requested. The worker will stop at a safe point."); refresh(); }
  }
  return <article className="job-row"><div className="title-line"><h3>{job.resource_name || job.resource_id}</h3><Badge state={job.state} /><span className="muted">{t(job.scan_mode.replaceAll("_", " "))}</span></div><p className="muted">{t(stage)}{t(" · Queued ")}{date(job.queued_at)}</p><div className="progress-line"><progress max="100" value={progress} aria-label={t("Scan progress for {p0}", { p0: job.resource_name })} /><span>{progress}%</span></div><dl className="job-stats">{([["Discovered", job.discovered], ["Unchanged", job.skipped_unchanged], ["Fetched", job.fetched], ["Parsed", job.parsed], ["Chunked", job.chunked], ["Embedded", job.embedded], ["Failed documents", job.failed_docs], ["Chunks written", job.chunks_written], ["Bytes read", job.bytes_read]] as const).map(([label, value]) => <div key={label}><dt>{t(label)}</dt><dd>{value ?? 0}</dd></div>)}</dl>{job.error_detail && <p className="feedback error">{t(job.error_detail)}</p>}<div className="button-row"><button onClick={() => setEventsOpen(!eventsOpen)} aria-expanded={eventsOpen}>{eventsOpen ? t("Hide events") : t("Inspect events")}</button>{active(job.state) && <button disabled={action.busy || job.is_cancel_requested} onClick={cancel}>{job.is_cancel_requested ? t("Cancellation requested") : action.busy ? t("Requesting…") : t("Cancel scan")}</button>}<small className="path">{job.id}</small></div><Feedback error={action.error} notice={action.notice} />{eventsOpen && <Events jobId={job.id} />}</article>;
}

function Events({ jobId }: { jobId: string }) {
  const { t, date } = useI18n();
  const events = useQuery<Items<JobEvent>>(`/jobs/${segment(jobId)}/events`, 2000);
  return <div className="events"><h4>{t("Job events")}</h4><Feedback error={events.error} retry={events.refresh} />{events.loading ? <p role="status">{t("Loading events…")}</p> : !events.data?.items.length ? <p className="muted">{t("No events recorded yet.")}</p> : <ol>{events.data.items.map((event) => <li key={event.id}><div><time>{date(event.logged_at)}</time><span className="event-level">{t(event.level)}</span><span>{event.stage && <>{t(event.stage)} · </>}{event.code}</span></div><p>{event.message}</p>{event.doc_uri && <p className="path">{event.doc_uri}</p>}</li>)}</ol>}</div>;
}

function OperationRow({ operation, refresh }: { operation: Operation; refresh: () => void }) {
  const { t, date } = useI18n();
  const action = useAction();
  async function retry() {
    const result = await action.run<Operation>(`/operations/${segment(operation.id)}/retry`, {});
    if (result) { action.setNotice("Retry queued."); refresh(); }
  }
  const retryable = operation.kind.startsWith("memory.")
    ? operation.state === "failed"
    : ["failed", "completed_with_errors", "cancelled"].includes(operation.state);
  return <article className="operation-row"><div className="title-line"><h3>{t(operation.kind.replaceAll("_", " "))}</h3><Badge state={operation.state} /></div><p className="muted">{t("Queued ")}{date(operation.queued_at)}{operation.finished_at != null && t(" · Finished {p0}", { p0: date(operation.finished_at) })}</p>{operation.error && <p className="reason">{t(operation.error)}</p>}<details><summary>{t("Operation details")}</summary><dl><dt>{t("ID")}</dt><dd className="path">{operation.id}</dd><dt>{t("Resource")}</dt><dd className="path">{operation.resource_id || "—"}</dd><dt>{t("Job")}</dt><dd className="path">{operation.job_id || "—"}</dd><dt>{t("Result")}</dt><dd><pre>{JSON.stringify(operation.result, null, 2)}</pre></dd></dl>  </details>{retryable && <button disabled={action.busy} onClick={retry}>{action.busy ? t("Retrying…") : t("Retry operation")}</button>}<Feedback error={action.error} notice={action.notice} /></article>;
}
