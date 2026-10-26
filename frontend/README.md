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
- **Knowledge chat:** create and revisit persisted conversations, select resources, choose retrieval mode, and inspect numbered source excerpts. Empty resource selection means all accessible resources. Search documents works without a chat model. Chat is synchronous and shows a waiting state; history is reloaded even after failed model calls.
- **Memory:** off by default and never fetched until the user opens the saved Memory section. Opting in can send user statements to configured mem0 models and persist facts. Forgetting a fact requires confirmation.

Configure `[model]`, `[embedding]`, and optional `[memory]` providers in the backend's `settings.toml`. This interface neither reads nor edits credentials. Local-first storage does not imply that configured model providers run locally.

## API contract

All fetches use relative `/api/v1` paths. List endpoints return `{items: [...]}`. Resource mutations, scans, retries, and index cleanup return an Operation with HTTP 202; conversations return HTTP 201 on creation, and message creation returns the persisted Message after the model call. IDs are URL-encoded. All `*_at` timestamps are numeric Unix seconds; the shared date formatter multiplies by 1,000 before constructing a JavaScript Date. Missing or invalid dates display `—`; API list ordering is preserved without client-side date sorting. Scan progress is a fraction from 0 to 1. Cancellation returns a Job; memory deletion returns `{deleted: true}`. Errors support `{error: {code, message, hint?}}` and FastAPI validation `detail`.

Endpoints used: `/status`, `/connectors`, `/resources` and its verify/pause/resume/revoke/scans actions, `/jobs` and events/cancel, `/operations` and retry, `/index/cleanup`, `/conversations` and messages, `/search`, `/memories`. The local folder connector uses `config.root_path`. Document paths and source content render as text, never raw HTML or executable file links.

## Validation

```powershell
npm run lint
npm run typecheck
npm run build
```

Installed Next.js canary guidance lives in `node_modules\next\dist\docs`; read it before framework changes, as required by `AGENTS.md`.
