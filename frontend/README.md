# Shovel workbench

A local-first, light workbench for local resources, background tasks, and knowledge chat. All runtime data comes from the backend; the interface contains no sample resources or generated answers.

## Development

From `frontend\`:

```powershell
npm ci
$env:SHOVEL_BACKEND_HOST = "127.0.0.1"
$env:SHOVEL_BACKEND_PORT = "9999"
npm run dev
```

Open `http://localhost:3000`. Development alone rewrites `/api/*` to the HTTP backend at the specified host and port (defaults shown above). Folder paths refer to the backend machine, not the browser. Windows paths such as `C:\Users\you\Documents` are preserved as entered.

## Production

```powershell
npm run build
```

This writes a static application to `frontend\out`. Serve those files from the backend host, with `/api/v1/*` handled by the backend on the same origin. No Node.js server, `next start`, server actions, runtime Next rewrites, external fonts, or server-rendered dynamic routes are needed in production. The build does not contact the Shovel API.

## Workflows

- **Resources:** add a named local folder with an optional description. Creation requests verification but never starts a scan. Verify, pause/resume, revoke, or explicitly confirm deletion. Configure incremental, full-sweep, or targeted scans; embeddings default to the runtime's configured capability. Keyword indexing works without vectors.
- **Background tasks:** inspect scan stages, counters and document events, request cancellation, inspect/retry operations, and queue index cleanup. Resources, jobs, operations and visible events refresh about every two seconds without clearing forms. Requests are serialized per poller and cancelled on unmount.
- **Knowledge chat:** create and revisit persisted conversations, select resources, choose retrieval mode, and inspect numbered source excerpts. Empty resource selection means all accessible resources. The official AG-UI `HttpAgent` SDK streams the MAF Agent's text and tool activity; history is reloaded after success or failure. Search documents works without a chat model.
- **Memory:** off by default. Opting in sends user statements to configured mem0 models for separate conversation-short-term and workspace-long-term facts. Deleting a conversation clears its short-term facts but keeps long-term facts. Saved Memory lists long-term facts; forgetting one requires confirmation and does not erase any short-term copy.

Configure `[model]`, `[embedding]`, and optional `[memory]` providers in the backend's `settings.toml`. This interface neither reads nor edits credentials. Local-first storage does not imply that configured model providers run locally.

## API contract

All fetches use relative `/api/v1` paths. List endpoints return `{items: [...]}`. Resource mutations, scans, retries, and index cleanup return an Operation with HTTP 202; conversations return HTTP 201 on creation, and message creation returns the persisted Message after the model call. IDs are URL-encoded. All `*_at` timestamps are numeric Unix seconds; the shared date formatter multiplies by 1,000 before constructing a JavaScript Date. Missing or invalid dates display `—`; API list ordering is preserved without client-side date sorting. Scan progress is a fraction from 0 to 1. Cancellation returns a Job; memory deletion returns `{deleted: true}`. Errors support `{error: {code, message, hint?}}` and FastAPI validation `detail`.

Chat uses `POST /agent` with AG-UI `threadId`/`runId`, the current user message,
and scope/Memory options in `forwardedProps`. Only `RUN_FINISHED` confirms
persistence; `RUN_ERROR` and interrupted connections require inspecting history
before resending. Switching conversations cancels the previous UI subscription.
No CopilotKit/Next.js server runtime is needed.

Endpoints used: `/status`, `/connectors`, `/resources` and its verify/pause/resume/revoke/scans actions, `/jobs` and events/cancel, `/operations` and retry, `/index/cleanup`, `/conversations` and messages, `/agent`, `/search`, `/memories`. The local folder connector uses `config.root_path`. Document paths and source content render as text, never raw HTML or executable file links.

## Validation

```powershell
npm run lint
npm run typecheck
npm run build
```

Installed Next.js canary guidance lives in `node_modules\next\dist\docs`; read it before framework changes, as required by `AGENTS.md`.
