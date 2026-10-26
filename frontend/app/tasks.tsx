"use client";

import { useState } from "react";
import { active, date, segment, useAction, useQuery, type Items, type Job, type JobEvent, type Operation, type Resource } from "./api";
import { Badge, Empty, Feedback } from "./ui";

export function Tasks({ resources }: { resources: Resource[] }) {
  const jobs = useQuery<Items<Job>>("/jobs", 2000);
  const operations = useQuery<Items<Operation>>("/operations", 2000);
  const action = useAction();
  const [cleanup, setCleanup] = useState(false);
  const [resourceId, setResourceId] = useState("");
  async function clean() {
    const result = await action.run<Operation>("/index/cleanup", resourceId ? { resource_id: resourceId } : {});
    if (result) { action.setNotice("Index cleanup queued."); setCleanup(false); operations.refresh(); }
  }
  return <section>
    <div className="section-toolbar"><div><h2>Scan jobs</h2><p>Progress updates every two seconds while this view is open.</p></div><button onClick={() => setCleanup(!cleanup)} aria-expanded={cleanup}>Clean up index…</button></div>
    {cleanup && <div className="inline-panel"><h3>Clean up index</h3><p>Run backend index maintenance for one resource or the whole index. Original files are not changed.</p><label>Scope<select value={resourceId} onChange={(event) => setResourceId(event.target.value)}><option value="">All resources</option>{resources.map((resource) => <option key={resource.id} value={resource.id}>{resource.name}</option>)}</select></label><div className="button-row"><button disabled={action.busy} onClick={clean}>{action.busy ? "Queuing…" : "Confirm cleanup"}</button><button onClick={() => setCleanup(false)} disabled={action.busy}>Cancel</button></div></div>}
    <Feedback error={action.error} notice={action.notice} />
    <Feedback error={jobs.error} retry={jobs.refresh} />
    {jobs.loading ? <p role="status">Loading scan jobs…</p> : jobs.data?.items.length === 0 ? <Empty title="No scans yet">Start a scan from Resources. Its stages and document events will appear here.</Empty> : jobs.data?.items.map((job) => <JobRow key={job.id} job={job} refresh={jobs.refresh} />)}
    <div className="section-toolbar separated"><div><h2>Operations</h2><p>Resource changes and index maintenance, including failed requests.</p></div></div>
    <Feedback error={operations.error} retry={operations.refresh} />
    {operations.loading ? <p role="status">Loading operations…</p> : operations.data?.items.length === 0 ? <Empty title="No operations yet">Resource actions will be recorded here.</Empty> : <div className="operation-list">{operations.data?.items.map((operation) => <OperationRow key={operation.id} operation={operation} refresh={operations.refresh} />)}</div>}
  </section>;
}

function JobRow({ job, refresh }: { job: Job; refresh: () => void }) {
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
  return <article className="job-row"><div className="title-line"><h3>{job.resource_name || job.resource_id}</h3><Badge state={job.state} /><span className="muted">{job.scan_mode.replaceAll("_", " ")}</span></div><p className="muted">{stage} · Queued {date(job.queued_at)}</p><div className="progress-line"><progress max="100" value={progress} aria-label={`Scan progress for ${job.resource_name}`} /><span>{progress}%</span></div><dl className="job-stats">{[["Discovered", job.discovered], ["Unchanged", job.skipped_unchanged], ["Fetched", job.fetched], ["Parsed", job.parsed], ["Chunked", job.chunked], ["Embedded", job.embedded], ["Failed documents", job.failed_docs], ["Chunks written", job.chunks_written], ["Bytes read", job.bytes_read]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? 0}</dd></div>)}</dl>{job.error_detail && <p className="feedback error">{job.error_detail}</p>}<div className="button-row"><button onClick={() => setEventsOpen(!eventsOpen)} aria-expanded={eventsOpen}>{eventsOpen ? "Hide events" : "Inspect events"}</button>{active(job.state) && <button disabled={action.busy || job.is_cancel_requested} onClick={cancel}>{job.is_cancel_requested ? "Cancellation requested" : action.busy ? "Requesting…" : "Cancel scan"}</button>}<small className="path">{job.id}</small></div><Feedback error={action.error} notice={action.notice} />{eventsOpen && <Events jobId={job.id} />}</article>;
}

function Events({ jobId }: { jobId: string }) {
  const events = useQuery<Items<JobEvent>>(`/jobs/${segment(jobId)}/events`, 2000);
  return <div className="events"><h4>Job events</h4><Feedback error={events.error} retry={events.refresh} />{events.loading ? <p role="status">Loading events…</p> : !events.data?.items.length ? <p className="muted">No events recorded yet.</p> : <ol>{events.data.items.map((event) => <li key={event.id}><div><time>{date(event.logged_at)}</time><span className="event-level">{event.level}</span><span>{event.stage} · {event.code}</span></div><p>{event.message}</p>{event.doc_uri && <p className="path">{event.doc_uri}</p>}</li>)}</ol>}</div>;
}

function OperationRow({ operation, refresh }: { operation: Operation; refresh: () => void }) {
  const action = useAction();
  async function retry() {
    const result = await action.run<Operation>(`/operations/${segment(operation.id)}/retry`, {});
    if (result) { action.setNotice("Retry queued."); refresh(); }
  }
  return <article className="operation-row"><div className="title-line"><h3>{operation.kind.replaceAll("_", " ")}</h3><Badge state={operation.state} /></div><p className="muted">Queued {date(operation.queued_at)}{operation.finished_at != null && ` · Finished ${date(operation.finished_at)}`}</p>{operation.error && <p className="reason">{operation.error}</p>}<details><summary>Operation details</summary><dl><dt>ID</dt><dd className="path">{operation.id}</dd><dt>Resource</dt><dd className="path">{operation.resource_id || "—"}</dd><dt>Job</dt><dd className="path">{operation.job_id || "—"}</dd><dt>Result</dt><dd><pre>{JSON.stringify(operation.result, null, 2)}</pre></dd></dl></details>{["failed", "completed_with_errors", "cancelled"].includes(operation.state) && <button disabled={action.busy} onClick={retry}>{action.busy ? "Retrying…" : "Retry operation"}</button>}<Feedback error={action.error} notice={action.notice} /></article>;
}
