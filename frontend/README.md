# Shovel workbench

A local-first, light workbench for local resources, background tasks, and knowledge chat. All runtime data comes from the backend; the interface contains no sample resources or generated answers.

The geometric shovel logo uses the existing teal `#086e75` palette.
`app/icon.svg` is the shared source for the browser icon, sidebar brand and
Shovel message avatars. Static image imports keep these assets available in
both development and the Python-served production export, without an image
optimization server. See [the design archive](../Documents/branding/README.md).

Choose **English** or **简体中文** in the **Language** tab in **Settings**.
English is the default; your choice is saved in this browser and restored
after reload. Switching languages preserves the current Session and unsaved
editor drafts. Navigation, forms, confirmations, status labels and accessibility
text are localized. Document excerpts, model replies, user-authored content,
identifiers, configuration JSON and unrecognized backend diagnostics stay
unchanged. If browser storage is unavailable, the selector reports that the
preference cannot be saved.

## Development

From `frontend\`:

```powershell
npm ci
$env:SHOVEL_BACKEND_HOST = "127.0.0.1"
$env:SHOVEL_BACKEND_PORT = "9999"
npm run dev
```

Open `http://localhost:3000`. Development alone rewrites `/api/*` to the HTTP backend at the specified host and port (defaults shown above). Folder paths refer to the backend machine, not the browser. Windows paths such as `C:\Users\you\Documents` are preserved as entered.

Keep Tailwind directives in `app\tailwind.css` and handwritten layout rules in
`app\globals.css`, imported in that order. This keeps incremental layout edits
outside the Tailwind loader's cached compiler so development serves fresh CSS.

With `uv run shovel run dev`, `[shovel.chat]` browser diagnostics are forwarded
to the terminal with the `web` prefix. A shared `run_id` correlates submission,
HTTP status and backend request ID, first text, tool activity, saved-message
confirmation and completion/failure/cancellation with backend Agent/RAG/Memory
stages. Logs contain IDs, counts and timings, not message content, source text,
Memory facts, tool arguments/results or raw errors. Production emits none of
these additional browser diagnostics; routine polling remains quiet.

Identical in-flight JSON GETs share one transport, including development Strict
Mode cleanup/re-subscribe. Each reader retains its own cancellation and response
body; once all readers cancel, the underlying read is aborted after a one-turn
grace period. Completed/error responses are not cached. Mutations invalidate
shared-read entries, and Agent streams remain incremental and unshared.
Settings shows startup warmup states and foreground chat capacity from status.
Only loopback Ollama models are automatically warmed with synthetic input;
remote providers are untouched. Background extraction waits for foreground idle.

## Production

```powershell
npm run build
```

This writes a static application to `frontend\out`. Serve those files from the backend host, with `/api/v1/*` handled by the backend on the same origin. No Node.js server, `next start`, server actions, runtime Next rewrites, external fonts, or server-rendered dynamic routes are needed in production. The build does not contact the Shovel API.

## Workflows

The sidebar has four groups in order: **Resource Management** (Resources,
Background tasks, Search Documents), **Development** (Skills, Workflows),
**Services** (Proactive Services, Approval Center, Activity), and **Chat** (New Session
and session history). The first three groups are collapsed initially and expand
independently; Chat stays expanded by default. Runtime connection status appears
beside **Personal Agent Platform** below the Shovel logo; Settings remains at the
bottom, and language selection lives only in **Settings → Language**.

The desktop sidebar defaults to **325px**, bounded by a 200px minimum and a maximum
of the smaller of 420px and 40% of the viewport width. Drag its right edge to resize,
or focus the separator and use Left/Right for 10px steps and Home/End for the limits.
Double-click or Enter restores the default. At viewport widths of 760px or less,
navigation moves above the main content and spans the page width.

- **Resources:** add a named local folder, classic Outlook folders or a public website with an optional description. Creation requests verification but never starts a scan. Verify, pause/resume, revoke, or explicitly confirm deletion. Configure incremental, full-sweep, or targeted scans; embeddings default to the runtime's configured capability. Keyword indexing works without vectors.
- **Background tasks:** inspect scan stages, counters and document events, request cancellation, inspect/retry operations, and queue index cleanup. Resources, jobs, operations and visible events refresh about every two seconds without clearing forms. Requests are serialized per poller and cancelled on unmount. Periodic queries pause while the browser tab is hidden and refresh on return; in-flight reads may finish, and explicit actions/Agent streams are not interrupted. Resources poll only in Resources, Background tasks (for cleanup scope), and Search Documents, never in Chat or Settings.
- **Search Documents:** an independent menu below Background tasks. Select resources, retrieval mode, Top N (1–30), and relevance/title sort direction. Sorting applies only to the returned Top N passages. Empty resource selection means all accessible resources. Search works without a chat model or Memory.
- **Chat:** the session tree is expanded by default and stays visible while visiting other pages. New Session and existing sessions are indented children of Chat, without a separate Sessions heading. Enter Chat to open it; click Chat again to collapse or expand its children without interrupting the active chat. New Session creates and selects a persisted session; history is ordered by latest interaction in a bounded scroll area, with Load more sessions for older pages. Rename/delete controls live under the selected Session in the sidebar. The Session page contains message history, numbered source excerpts, and a multiline composer: Enter inserts a newline; Ctrl + Enter or Send Message submits. The official AG-UI `HttpAgent` SDK streams the MAF Agent's text and tool activity; message and Session history are refreshed after success or failure.
- **Settings:** the bottom sidebar menu opens five keyboard-accessible tabs: Runtime configuration, Manage long-term Memory, MCP Server Management, Agent instructions, and Language. Switching tabs preserves mounted editors and drafts. Runtime configuration includes provider readiness and performance/warmup; provider changes require editing settings.toml and restarting the backend. Saved facts load from SQLite only on request. Memory shows its Zvec index status and pending operations, supports confirmed soft deletion, and can queue a rebuild in Background tasks. Session history, session summaries and long-term facts remain separate; session deletion removes its summary/session-scoped Memory, not cross-session facts.

On desktop, Chat uses a compact, single-line heading and a viewport-height panel:
messages scroll independently above the composer, without the former 660px
message-height cap or a decorative page footer. Message cards and their text use
the available scroll width with responsive padding; Shovel has a teal brand avatar,
while user messages have a person icon and a shaded background. Pending and streamed
replies use the same role styling, and source excerpts remain expandable.

### Message formats and attachments

The Markdown composer provides heading, emphasis, list, quote, link, table,
code, formula and Mermaid insertion tools, plus an **Edit / Preview** switch.
Its format selector also accepts plain text, JSON, CSV, HTML, LaTeX, Mermaid
and code directly, wrapping the submitted text in an appropriate fenced block.
The submitted message, including fences, is limited to 16,000 characters.
Composer drafts (text, format, preview and attachment identities) stay in
page memory, isolated by workspace and Session, when switching Sessions or
visiting Settings. In-flight uploads continue; returning does not upload the
same file again. Failed/uncertain sends retain their drafts, successful sends
clear only the unchanged originating draft, and deleting a Session discards
its draft and cancels outstanding uploads. Expired attachments block sending
and must be removed/re-attached. Drafts are not written to browser storage;
reloading or closing the page discards them.
Both user messages and replies render Markdown/GFM tables, highlighted code and
LaTeX. **View source / Preview** switches preserve the original message, and
**Copy source** copies it without copying rendered markup. Streaming replies
render Markdown incrementally; HTML/CSV/Mermaid block previews wait for completion.

HTML is untrusted content, not an application: inline HTML is allowlist-sanitized;
fenced HTML previews are sanitized inside sandboxed, opaque-origin frames with
a restrictive CSP. Scripts, forms, navigation and remote resources are disabled.
Mermaid uses strict mode, bounded input/edges, no user configuration, HTML,
image/icon resources or interactive links, sanitized SVG and the same isolated
frames. Resource syntax is rejected before Mermaid's host-DOM layout as well.
KaTeX disables trusted
commands and limits expansion. Invalid or oversized previews report an error or
show source rather than executing content. CSV cells are text, not formulas.
CSV previews are limited to 200 rows and 40 columns; messages over 100 KB display
as source to bound parsing work.

Only explicit HTTP(S) links without embedded credentials are clickable; they
open a separate tab with no opener or referrer. Review the destination before
following it. Remote images are not automatically fetched, avoiding tracking and
unexpected requests. Uploaded images use Session-scoped local URLs, fit within
message cards and can be opened in a separate tab. Source excerpts remain plain
text and are not interpreted as message markup.

**Attach files** selects files, and pasting an image from the clipboard uploads
it immediately. Upload progress/errors are visible and sending waits for all
selected uploads to succeed; failed uploads can be retried with the same upload
identity or removed. Each message accepts up to five files of at most 10 MiB
each. These are temporary attachments, not new indexed resources. Sending
attachments shares their supported contents with the configured model, which
may be remote; only upload content you are authorized to share.

Uploads are stored under the backend's `temp_path` (default `~/.shovel/tmp`,
also created by `shovel init`; override with `SHOVEL_TEMP_PATH`). Database and
workspace identities select a private namespace; client filenames are never
storage paths. Uploads remain local until sending. Pending uploads expire after
24 hours; sending establishes a seven-day expiry, which reusing an attachment
does not extend. Startup and the existing Runtime worker clean expired files;
Session deletion removes its files immediately. Expired attachment metadata
remains in history, but downloading returns 410 and the model receives an
explicit unavailable marker instead of invented content. Removing an already
sent file from an editor draft does not delete the history's copy.

Supported uploads are PNG/JPEG/GIF/WebP, UTF-8 text/Markdown/JSON/CSV/TSV/HTML/XML,
common source/configuration files, and DOCX/PPTX/XLSX. PDF, SVG, executable
binaries and archives are deliberately not accepted. Images are decoded within
a 16-million-pixel bound, sanitized, oriented, resized to at most 2048×2048 and
converted to JPEG; metadata and animation are removed. Previews and downloads
use those sanitized bytes, not the original image. Text/Office attachments
contribute up to 8,000 characters per file to model context; they are untrusted
reference data, not instructions. Image interpretation requires a vision-capable
model and unsupported vision fails visibly. Attachments are also available in
retained model history until expiry, subject to existing context limits, but
are not automatically fed into Memory extraction.

Model input has separate bounded budgets: `model.max_input_chars` continues
to count text, instructions, serialized references/tool results and image
metadata, while `model.max_input_images` (default 5) and
`model.max_input_image_bytes` (default 10 MiB total image-file bytes before base64)
bound inline images across the current input and retained history. Base64
transport expansion is not text context. Current images exceeding media limits
fail explicitly before model transport; older history may be omitted with a
warning to stay within the remaining budgets. These are safety/admission limits,
not provider-specific vision token estimates. Unsupported/non-image payloads
do not bypass the character guard.

Uploads and content preparation have bounded concurrency. Image/Office
processing and filesystem work run off the Host event loop; slow upload streams
do not hold the attachment commit lock or Session chat lock. Final quota checks,
file installation and metadata commit remain serialized, including cancellation
and Session deletion.

Additional server quotas are twenty pending and one hundred active attachments
per Session, 100 MiB of stored bytes per Session and 1 GiB per runtime namespace.
Uploads are streamed with byte limits, validated by type, and support retrying
the same UUID only with identical contents. Only validated raster images may be
served inline; other files, including HTML, are forced downloads. Attachment
access inherits the Host's loopback/same-origin checks and is Session-scoped.
This is not a malware scanner or an enterprise-governed upload channel.

Every Session uses the full **Agent** with Memory, document retrieval, tools and
workspace extensions. The composer has no mode selector and sends no
`interaction_mode` field. Existing permission and approval checks still apply.

**Settings → MCP Server Management** lists the current workspace's saved servers and edits
`mcp.json` as JSON (stdio and Streamable HTTP, enabled/disabled, allowed tools,
environment and headers). Saving validates and atomically writes the file with
revision conflict protection, without opening connections or running commands.
The separate **Restart Agent** action closes cached clients; extensions load on
the next chat. It is rejected while any session is generating and retains
sessions, Memory and background tasks. Reloading a dirty editor asks before
discarding edits. Settings does not poll resources. Only configure trusted
servers and prefer `${ENV_VAR}` credential references: the editor shows the
actual file, including any inline secrets.

**Settings → Agent instructions** edits additional instructions in the workspace's
`agent-instructions.md`. Built-in citation, Memory and tool-use policies remain
visible and are always retained; custom text is appended to MAF's `instructions`.
**Save & restart Agent** atomically saves with revision conflict protection and
automatically replaces the Agent for the next message in existing or new sessions,
preserving conversations and Memory. Model and extension clients initialize on
demand. Saving is rejected during active conversations or model warmup, without
changing the file or interrupting work. Empty text restores built-in defaults.
The editor shows the character limit derived from `model.max_input_chars`;
isolated Memory extraction and summary prompts are unaffected. Reload asks before
discarding a dirty draft. After a lost response or saved-but-failed restart, reload
the file and retry **Restart Agent**. Never put credentials in these instructions.

Configure `[model]` for chat, optional `[memory.embedder]` for Memory semantic recall, and optional `[embedding]` for document vector search in the backend's `settings.toml`. Native Memory persists in SQLite without an embedder; unavailable semantic recall is explicitly reported instead of blocking chat. Provider credentials remain outside this interface; inline MCP credentials are visible in the MCP editor. Local-first storage does not imply local model inference.

**Resources → Web Search** accepts a canonical public website URL and crawl
limits. The URL fences the origin/path; the crawler discovers links and XML
Sitemaps without JavaScript execution or login. HTML and extensionless page URLs
follow links within their containing directory: `/zh-cn/guides/vision` crawls
within `/zh-cn/guides/`, not the entire site. Directory URLs must end in `/`
to restrict crawling to that subtree; for example, use `/docs/`, not `/docs`.
Non-HTML file URLs such as PDFs retain their own path scope.
The form configures page/depth,
request delay, retry attempts, download/time limits and optional query URLs.
**Crawl website** uses the existing durable scan endpoint and embedding toggle.
robots.txt and noindex/nofollow are respected; private networks and unsafe
redirects are blocked. Scan limits and failures are visible in Background tasks
events. Full sweeps do not infer deletion from missing pages, and the indexing
Document limit does not cap crawl requests. See the
[supported source boundaries](../README.md#supported-sources) and
[scan pipeline guide](../Documents/scan_pipeline.md) for discovery and recovery.

## Microsoft Learn MCP example

In **Settings → MCP Server Management**, merge this entry into the existing
`mcpServers` object, preserving any other servers:

```json
{
  "mcpServers": {
    "microsoft_learn": {
      "type": "http",
      "url": "https://learn.microsoft.com/api/mcp",
      "disabled": false,
      "allowed_tools": [
        "microsoft_docs_search",
        "microsoft_docs_fetch",
        "microsoft_code_sample_search"
      ]
    }
  }
}
```

The [official Microsoft Learn MCP server](https://learn.microsoft.com/en-us/training/support/mcp)
uses Streamable HTTP and requires no authentication or API key. The domain is
`learn.microsoft.com`, not `learning.microsoft.com`. Opening the MCP endpoint in
a browser can return HTTP 405; that is not a connection test.

Save the configuration, finish any active/queued chats, and use **Restart Agent**.
Saving alone does not connect to the server; the next chat loads its tools.
This config belongs to Shovel's workspace `mcp.json`, not Copilot CLI's
`.github\mcp.json`. HTTP MCP does not require Python extensions to be enabled.

In Chat, use these public-data-only prompts:

| Test | Prompt | Expected evidence |
| --- | --- | --- |
| Documentation search | Use `mcp_microsoft_learn_microsoft_docs_search` to find the official Azure Functions Python developer guide. Summarize the programming models and provide the official URLs. Do not use local document search. | A matching MCP tool event and Learn search results, followed by an answer with official links. |
| Complete article | Use `mcp_microsoft_learn_microsoft_docs_fetch` to read `https://learn.microsoft.com/en-us/training/support/mcp`. State its endpoint, transport and authentication requirements. | Endpoint `https://learn.microsoft.com/api/mcp`, Streamable HTTP, no authentication required. |
| Code samples | Use `mcp_microsoft_learn_microsoft_code_sample_search` with query `Azure Blob Storage Python upload_blob` and language `python`. Explain one returned sample and cite its source. | A code-sample MCP call with Python results mentioning `upload_blob`. |

An answer from model knowledge alone is not a passing MCP test: inspect the tool
activity, then wait for `RUN_FINISHED`. A model must support tool calling to run
these Chat checks; remote model providers may charge and receive prompt/tool
content. Do not include private documents, credentials or business data.

For a repeatable integration check, run from the repository root:

```powershell
$env:SHOVEL_RUN_LEARN_MCP_TESTS = "1"
try {
  uv run pytest backend\tests\test_agent_extensions.py::test_learn_mcp_tools_execute_through_shovel_agent --no-cov
} finally {
  Remove-Item Env:\SHOVEL_RUN_LEARN_MCP_TESTS
}
```

The three cases validate Shovel's configuration service, extension loader and
MAF Agent tool loop against the real public Learn MCP endpoint. Model responses
are deterministic fixtures, so no model credentials or paid inference are used.
Only fixed public queries/URLs are sent; the workspace is temporary and existing
settings/stores are untouched. These cases skip unless explicitly opted in.
They verify the integration, not a configured model's tool selection or the Web UI.
The server's current tools and schemas are discovered when the connection opens;
network/server failures remain test failures rather than silent skips.

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
Failed extraction offers bounded retry. Facts and extraction receipts commit
atomically; index failures have their own durable retry queue. Recent SQLite
history is immediately available; cross-session semantic recall becomes available
after extraction and indexing finish.

Endpoints used: `/status`, `/connectors`, `/resources` and its verify/pause/resume/revoke/scans actions, `/jobs` and events/cancel, `/operations` and retry, `/index/cleanup`, `/conversations` and messages, `/agent`, `/agent/mcp` (GET/PUT), `/agent/instructions` (GET/PUT, no-store), `/agent/restart` (POST), `/search`, `/memories`. The local folder connector uses `config.root_path`. Document paths and source content render as text, never raw HTML or executable file links.

## Validation

```powershell
npm run lint
npm run typecheck
npm run build
```

Installed Next.js canary guidance lives in `node_modules\next\dist\docs`; read it before framework changes, as required by `AGENTS.md`.
