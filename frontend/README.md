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

With `uv run shovel run dev`, `[shovel.chat]` browser diagnostics are forwarded
to the terminal with the `web` prefix. A shared `run_id` correlates submission,
HTTP status and backend request ID, first text, tool activity, saved-message
confirmation and completion/failure/cancellation with backend Agent/RAG/Memory
stages. Logs contain IDs, counts and timings, not message content, source text,
Memory facts, tool arguments/results or raw errors. Production emits none of
these additional browser diagnostics; routine polling remains quiet.

## Production

```powershell
npm run build
```

This writes a static application to `frontend\out`. Serve those files from the backend host, with `/api/v1/*` handled by the backend on the same origin. No Node.js server, `next start`, server actions, runtime Next rewrites, external fonts, or server-rendered dynamic routes are needed in production. The build does not contact the Shovel API.

## Workflows

- **Resources:** add a named local folder with an optional description. Creation requests verification but never starts a scan. Verify, pause/resume, revoke, or explicitly confirm deletion. Configure incremental, full-sweep, or targeted scans; embeddings default to the runtime's configured capability. Keyword indexing works without vectors.
- **Background tasks:** inspect scan stages, counters and document events, request cancellation, inspect/retry operations, and queue index cleanup. Resources, jobs, operations and visible events refresh about every two seconds without clearing forms. Requests are serialized per poller and cancelled on unmount. Periodic queries pause while the browser tab is hidden and refresh on return; in-flight reads may finish, and explicit actions/Agent streams are not interrupted. Resources poll only in Resources, Background tasks (for cleanup scope), and Search Documents, never in Chat or Settings.
- **Search Documents:** an independent menu below Background tasks. Select resources, retrieval mode, Top N (1–30), and relevance/title sort direction. Sorting applies only to the returned Top N passages. Empty resource selection means all accessible resources. Search works without a chat model or Memory.
- **Chat:** click the main navigation item to open or collapse the sidebar's Session section without interrupting the active chat. New Session creates and selects a persisted session; history is ordered by latest interaction in a bounded scroll area, with Load more sessions for older pages. Rename/delete controls live under the selected Session in the sidebar. The Session page contains message history, numbered source excerpts, and a multiline composer: Enter inserts a newline; Ctrl + Enter or Send Message submits. The official AG-UI `HttpAgent` SDK streams the MAF Agent's text and tool activity; message and Session history are refreshed after success or failure.
- **Settings:** the bottom sidebar menu contains runtime provider status and long-term Memory list/forget controls. Memory is enabled for every Session without a toggle. Short-term mem0 Memory stores original user messages without a duplicate LLM extraction; long-term mem0 Memory still extracts and merges user facts. Deleting a Session clears its short-term Memory but keeps long-term facts. Forgetting a long-term fact requires confirmation and does not erase its short-term copy.

**MCP Server Management** lists the current workspace's saved servers and edits
`mcp.json` as JSON (stdio and Streamable HTTP, enabled/disabled, allowed tools,
environment and headers). Saving validates and atomically writes the file with
revision conflict protection, without opening connections or running commands.
The separate **Restart Agent** action closes cached clients; extensions load on
the next chat. It is rejected while any session is generating and retains
sessions, Memory and background tasks. Reloading a dirty editor asks before
discarding edits. This page does not poll resources. Only configure trusted
servers and prefer `${ENV_VAR}` credential references: the editor shows the
actual file, including any inline secrets.

Configure `[model]` and `[memory]` for chat, and optional `[embedding]` for vector search, in the backend's `settings.toml`. Missing Chat/Memory configuration disables sending and directs users to Settings; Memory is never silently switched off. Provider credentials remain outside this interface; inline MCP credentials are visible in the MCP editor. Local-first storage does not imply that configured model providers run locally.

## API contract

All fetches use relative `/api/v1` paths. List endpoints return `{items: [...]}`. Resource mutations, scans, retries, and index cleanup return an Operation with HTTP 202; sessions return HTTP 201 on creation, and message creation returns the persisted Message after the model call. The Session interface retains the existing `/conversations` API paths and `conversation_id` wire field. IDs are URL-encoded. All `*_at` timestamps are numeric Unix seconds; the shared date formatter multiplies by 1,000 before constructing a JavaScript Date. Missing or invalid dates display `—`; Session history pages are deduplicated and ordered by numeric `updated_at`, while other API list ordering is preserved. Scan progress is a fraction from 0 to 1. Cancellation returns a Job; memory deletion returns `{deleted: true}`. Errors support `{error: {code, message, hint?}}` and FastAPI validation `detail`.

Chat uses `POST /agent` with AG-UI `threadId`/`runId`, the current user message,
and `memory_enabled: true` in `forwardedProps`, using automatic retrieval across
accessible resources. API requests also default to Memory when the field is omitted.
Only `RUN_FINISHED` confirms
persistence; `RUN_ERROR` and interrupted connections require inspecting history
before resending. Switching sessions cancels the previous UI subscription.
No CopilotKit/Next.js server runtime is needed.
The backend sends `Cache-Control: no-cache, no-transform` on AG-UI responses to
prevent the development proxy's gzip compression from buffering small SSE
frames. Early text is not confirmation that the message has been saved.
`RUN_FINISHED` confirms the durable answer and queued long-term Memory task,
not completed extraction. A compact message status shows the background update;
it pauses polling in hidden tabs and stops on completion/failure/cancellation.
Safe failures offer retry; potentially partial writes require review in
Settings rather than automatic replay. Short-term Memory is available immediately;
cross-Session long-term facts become available after extraction finishes.

Endpoints used: `/status`, `/connectors`, `/resources` and its verify/pause/resume/revoke/scans actions, `/jobs` and events/cancel, `/operations` and retry, `/index/cleanup`, `/conversations` and messages, `/agent`, `/agent/mcp` (GET/PUT), `/agent/restart` (POST), `/search`, `/memories`. The local folder connector uses `config.root_path`. Document paths and source content render as text, never raw HTML or executable file links.

## Validation

```powershell
npm run lint
npm run typecheck
npm run build
```

Installed Next.js canary guidance lives in `node_modules\next\dist\docs`; read it before framework changes, as required by `AGENTS.md`.
