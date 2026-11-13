# Shovel workbench

A local-first, light workbench for local resources, background tasks, and knowledge chat. All runtime data comes from the backend; the interface contains no sample resources or generated answers.

The geometric shovel logo uses the existing teal `#086e75` palette.
`app/icon.svg` is the shared source for the browser icon, sidebar brand and
Shovel message avatars. Static image imports keep these assets available in
both development and the Python-served production export, without an image
optimization server. See [the design archive](../Documents/branding/README.md).

Choose **English** or **简体中文** in the **General settings** tab in **Settings**.
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
bottom, and language selection lives only in **Settings → General settings**.

The desktop sidebar defaults to **325px**, bounded by a 200px minimum and a maximum
of the smaller of 420px and 40% of the viewport width. Drag its right edge to resize,
or focus the separator and use Left/Right for 10px steps and Home/End for the limits.
Double-click or Enter restores the default. At viewport widths of 760px or less,
navigation moves above the main content and spans the page width.

- **Resources:** add a named local folder, classic Outlook folders or a public website with an optional description. Creation requests verification but never starts a scan. Verify, pause/resume, revoke, or explicitly confirm deletion. Configure incremental, full-sweep, or targeted scans; embeddings default to the runtime's configured capability. Keyword indexing works without vectors.
- **Background tasks:** inspect scan stages, counters and document events, request cancellation, inspect/retry operations, and queue index cleanup. Resources, jobs, operations and visible events refresh about every two seconds without clearing forms. Requests are serialized per poller and cancelled on unmount. Periodic queries pause while the browser tab is hidden and refresh on return; in-flight reads may finish, and explicit actions/Agent streams are not interrupted. Resources poll only in Resources, Background tasks (for cleanup scope), and Search Documents, never in Chat or Settings.
- **Search Documents:** an independent menu below Background tasks. Select resources, retrieval mode, Top N (1–30), and relevance/title sort direction. Sorting applies only to the returned Top N passages. Empty resource selection means all accessible resources. Search works without a chat model or Memory.
- **Chat:** the session tree is expanded by default and stays visible while visiting other pages. New Session and existing sessions are indented children of Chat, without a separate Sessions heading. Enter Chat to open it; click Chat again to collapse or expand its children without interrupting the active chat. New Session creates and selects a persisted session; history is ordered by latest interaction in a bounded scroll area, with Load more sessions for older pages. Rename/delete controls live under the selected Session in the sidebar. The Session page contains message history, numbered source excerpts, and a multiline composer: Enter inserts a newline; Ctrl + Enter or Send Message submits. The official AG-UI `HttpAgent` SDK streams the MAF Agent's text and tool activity; message and Session history are refreshed after success or failure.
- **Settings:** the bottom sidebar menu opens five keyboard-accessible tabs: Runtime configuration, Manage long-term Memory, MCP Server Management, Agent instructions, and General settings. Switching tabs preserves mounted editors and drafts. Runtime configuration includes provider readiness and performance/warmup; provider changes require editing settings.toml and restarting the backend. Saved facts load from SQLite only on request. Memory shows its Zvec index status and pending operations, supports confirmed soft deletion, and can queue a rebuild in Background tasks. Session history, session summaries and long-term facts remain separate; session deletion removes its summary/session-scoped Memory, not cross-session facts.

**General settings** also saves an optional nickname, avatar, gender, occupation
and biography in the current workspace/user's SQLite profile. Avatars accept
static PNG/JPEG/WebP up to 2 MB and 4 megapixels; the backend resizes them to at
most 256 pixels and removes metadata. Replacing or removing an avatar atomically
replaces its stored bytes; no orphan files remain.
Profile changes atomically update settings-managed Profile Memory and its vector
outbox. Startup and Agent restart reconcile the projection without duplicate
facts. Edit or clear these facts in General settings, not Memory management;
chat extraction cannot override them. The next Agent request reads the current
profile, even with automatic Memory off, and prefers the saved nickname.
Avatar bytes are never sent to models. Historical and pending user messages
display the current nickname/avatar, without rewriting stored message content.
Profile reads refresh across tabs while preserving unsaved editor drafts;
conflicting saves require reloading rather than overwriting newer revisions.

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
While any retained Session has text or attachments (including cancelled/failed
uploads), the page registers a native refresh/close warning. Clearing or
successfully sending the last unchanged draft removes that listener; navigating
between Sessions or workbench views does not discard drafts or show a warning.
This is not persistence: browsers control the wording and may suppress the
dialog without prior interaction, on mobile app termination or on a crash.
Both user messages and replies render Markdown/GFM tables, highlighted code and
LaTeX. **View source / Preview** switches preserve the original message, and
**Copy source** copies it without copying rendered markup. Streaming replies
render Markdown incrementally; HTML/CSV/Mermaid block previews wait for completion.
Preview moves keyboard focus to the rendered composer; Escape or **Edit**
restores the editor selection and focus. Code-like formats disable spelling,
autocorrection and capitalization. Formatting that exceeds the input limit
reports an inline error rather than silently doing nothing. Touch layouts use
at least 44-pixel composer/message control targets. Isolated previews have a
keyboard-operable height slider (160–800 pixels, capped at 70% of viewport height);
iframe sandbox permissions are unchanged.

JSON mode offers explicit **Validate JSON** and **Format JSON** actions. Invalid
input is retained, and formatting changes whitespace only: numeric lexemes,
large integers, duplicate keys and strings stay exact. Tools accept at most
100,000 source/output characters and 64 nesting levels; the composer still
enforces its 16,000-character limit. JSON message blocks can toggle a formatted
view without changing the saved message or **Copy source** result. Code language
selection applies to Code mode fences and Markdown code insertion, survives
Session navigation, and participates in the same preview/send-budget input.
Images and completed Mermaid diagrams also offer an **Enlarge** viewer with
50–300% keyboard zoom, reset, Escape/close and focus return. Native modal focus
containment and the diagram's opaque-origin sandbox/CSP are retained; enlargement
does not enable external resources or change source copying.

History remains paginated to 100 messages rather than virtualizing selectable
text or changing scroll anchors. Memoized history rows avoid reparsing unchanged
messages while typing/streaming. Text updates coalesce over 50 milliseconds,
flushing the latest cumulative value on completion; durable completion still
requires AG-UI confirmation. History attachment cards share one 30-second expiry
clock, removed when unused. HTML/Mermaid previews start within 200 pixels of
the viewport, use one serial renderer, and reuse sanitized results from a
16-entry / two-million-character LRU cache. At most 32 previews may be
active/queued; overflow reports an error with source viewing or retry available.
Unmounted/changed previews discard stale results. These bound repeated work,
not initial history layout or arbitrary diagram complexity.

The composer requests a debounced, cancellable send-budget estimate through
`POST /api/v1/conversations/{id}/budget-preview`. It uses the same server-side
attachment preparation, media counting and initial text reservation as a send:
formatted message, loaded instructions, current profile, selected Working Memory,
prepared excerpts, image bytes, and 1,012 characters of framing/context reserve.
Candidate history counts are shown separately, not added as mandatory input.
Memory recall, RAG, tool declarations/results and final history selection remain
dynamic; a passing estimate is not a promise that a later model call will fit.
No model/embedding requests, evidence retrieval, message writes, file binding
or TTL extension happen during preview. Only aggregate counts leave the server.
Changing text, format, attachments or selected context invalidates the displayed
estimate immediately. Pending/failed estimates and known excess input disable
send; retry is explicit. Shorten text or remove files using the existing controls.
There is no silent truncation/downsampling and no bypass of the final model gate.

### Browser regression checks

From `frontend\`, with the locked dependencies installed:

```powershell
npm run build
npx playwright install chromium
npm run test:browser
# Alternatively use an existing local Edge installation:
$env:PLAYWRIGHT_CHANNEL = "msedge"
npm run test:browser
Remove-Item Env:\PLAYWRIGHT_CHANNEL
```

The Playwright runner starts its own loopback-only static-export fixture server
on port 59992 and stops it after the run. An occupied port is an error; it never
reuses an existing Host. Each browser context has independent synthetic Sessions,
files and API/SSE responses held only in server memory. No real configuration,
user stores, external model services or upstream network requests are used.
The same tests run in desktop Chromium and mobile Chromium emulation. They cover
native dirty-draft reload protection, successful/failed sends, budget changes and
retry, mixed paste, upload cancellation/same-UUID recovery, reading scope,
streaming/pagination/late-growth scrolling, clipboard failure, keyboard send and
horizontal layout, preview focus/selection, touch targets, deferred/cached
diagrams, height controls and sandbox isolation. A synthetic 100-attachment /
240-chunk burst also checks the shared clock, bounded visible updates and the
complete persisted final answer. Additional cases cover exact JSON formatting/
copying, code-language draft/send consistency, image/diagram modal controls and
PDF page scope/failure presentation. These are frontend contract tests, not live model or full
backend acceptance; backend regressions independently verify the real preview
endpoint and quota/attachment isolation. CI runs both suites without paid calls.

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
common source/configuration files, DOCX/PPTX/XLSX, and controlled text-layer PDF.
SVG, executable
binaries and archives are deliberately not accepted. Images are decoded within
a 16-million-pixel bound, sanitized, oriented, resized to at most 2048×2048 and
converted to JPEG; metadata and animation are removed. Previews and downloads
use those sanitized bytes, not the original image. Text/Office/PDF attachments
contribute up to 8,000 characters per file to model context; they are untrusted
reference data, not instructions. Image interpretation requires a vision-capable
model and unsupported vision fails visibly. Attachments are also available in
retained model history until expiry, subject to existing context limits, but
are not automatically fed into Memory extraction.

New Office attachments use versioned structured excerpts: DOCX retains joined
runs, paragraphs and table row/cell boundaries; PPTX follows presentation slide
order; XLSX follows worksheet order/names, resolves shared/inline strings and
labels coordinates, booleans, errors, stored formulas and cached results.
Formulas are never evaluated; missing expressions/caches are marked unavailable,
and shared formulas are not reconstructed. Number/date styling, merged-cell
layout, charts, speaker notes and full Office visual fidelity are not provided.
Package limits and XML/relationship fences remain enforced; extraction never
follows external relationships or fetches external data. Existing attachments
without `reading_scope.office_extraction = "structured-v1"` retain the legacy
flattened extraction so saved excerpt statistics are not silently reinterpreted.
Re-upload an old file to obtain structured excerpts.

PDF uses the existing locked `pypdf` dependency in a disposable local process,
not the knowledge-ingestion parser or a browser PDF renderer. Each parse has
a 15-second wall limit and 512 MiB process-memory ceiling (Windows Job Object
or Linux address-space limit; Linux also limits CPU time to 10 seconds).
Unavailable OS limits fail closed with 503; other platforms are not currently
supported. At most two attachment processors run per Host, outside database/
Session commit locks; timeout terminates and reaps the child. These are resource
fences, not an OS filesystem/network sandbox or malware scanner.

PDF must be at most 10 MiB and 50 pages. Bounded strict parsing limits object
graphs, page trees, stream/filter/operation work and decoded streams (4 MiB).
Encrypted, form/action/embedded-file PDFs, malformed/over-limit documents and
documents without extractable text fail explicitly. No password guessing, OCR,
JavaScript, external relationship fetching, image rendering or external tools
are used. Text has page markers and the shared 8,000-character excerpt limit;
scope reports total/processed pages, not proof the model read the entire file.
Original PDF bytes are forced downloads with no inline PDF viewer, and remain
untrusted if opened externally. PDF uses the same Session isolation, retry
identity, expiry, quotas, non-Memory behavior and model text-budget gates.
Backend regressions exercise real PDF bytes and the isolated worker; browser
PDF tests exercise only the synthetic upload/API presentation contract.

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

Uploads and content preparation have bounded concurrency. Image/Office/PDF
processing and filesystem work run off the Host event loop; slow upload streams
do not hold the attachment commit lock or Session chat lock. Final quota checks,
file installation and metadata commit remain serialized, including cancellation
and Session deletion.

### Dependency audit disposition (2026-11-13)

The initial online audits reported **15 affected package entries** (7 high,
8 moderate), including **6 high production entries**. These are package counts,
not 15 independent or demonstrated application exploits. Targeted compatible
updates retain Next/eslint-config-next `16.4.0-canary.20`:

| Advisory family | Resolution / disposition |
| --- | --- |
| Zod, GHSA-mrhx-c7xc-m3w5 | AG-UI client/internal packages 1.0.4 support the patched Zod peer; direct Zod 4.6.7 removes the vulnerable 3.25.76 / 4.5.4 branches. AG-UI transport/schema regressions remain required. |
| Sharp/librsvg, GHSA-wq5f-xc86-pv6w | Updated 0.35.4 to 0.35.5 within Next's range. No malicious-SVG decode path was established in the static Python-served production export; reassess if server-side image processing is introduced. |
| source-map-js, GHSA-68fv-2mgg-jv7q | Updated 1.2.1 to 1.2.2 in Next/PostCSS/Tailwind tooling. No user-document source-map parsing path was identified. |
| brace-expansion, GHSA-q2hr-2g5m-vwhr / GHSA-qhr7-859c-m2p7 / GHSA-6j4f-fj2g-mc7p | Development branches updated to patched 1.1.21 / 5.0.12 without major overrides. |
| minimatch, GHSA-g3hv-75wg-m249 | **Deferred, development-only:** typescript-eslint 8.70.0 retains minimatch 10.2.6. The advisory requires `optimizationLevel >= 2`; the inspected parser call passes only `{ dot: true }`, with no untrusted remote glob path identified. The available 11.0.1 fix is outside the upstream requested major; do not force that override or downgrade Next to satisfy an audit summary. |

The successful `npm update sharp source-map-js brace-expansion` online audit
reported **8 moderate entries, no high/critical entries**, consistent with
minimatch and its development ancestors. `npm ls --omit=dev minimatch
brace-expansion --all` contains neither package. This is **not a zero-findings
claim**: monitor the minimatch advisory and typescript-eslint's compatible
resolution; reopen the exception before enabling higher glob optimization or
accepting untrusted build/lint patterns, and at the next dependency refresh.

Interim standalone `npm audit --json` / `--omit=dev` attempts were blocked
by the configured proxy's HTTP 429; a public-endpoint attempt failed TLS.
TLS verification and registry configuration were not weakened. An offline
audit returned an empty report despite the known moderate findings and was
explicitly rejected as evidence. The final **online production audit succeeded
with exit 0 and zero entries**. The subsequent full standalone audit still hit
HTTP 429; its last successful post-upgrade all-dependency counts are the
8 moderate entries from the update audit above, not the offline zero report.
Re-run the full command when the audit service recovers, retaining nonzero
advisory/service failures and updating this record:

```powershell
npm audit --json
npm audit --omit=dev --json
npm ls @ag-ui/client zod sharp source-map-js brace-expansion minimatch --all
```

Disposition is scoped to the frontend dependency audit for issue #12, not an
exhaustive supply-chain/security assessment or proof of exploitability. No
blanket `npm audit fix --force`, major glob override or automatic install-script
approval was used.

### Attachment interaction and lifecycle

The composer reports actual upload transfer progress separately from server
validation/saving. Cancel stops the browser request but does not claim that a
server-side commit was undone: the draft retains its file and UUID for retry or
removal, and any already-received temporary file remains subject to expiry.
Retrying never assigns a new UUID. Text accompanying pasted images/files is
inserted at the current selection, with a visible notice if the message limit
truncates it; files retain clipboard order.

`GET /api/v1/conversations/{session}/attachments` supplies the allowed extensions,
upload/model-media limits, active availability and remaining storage/count
quotas. These are current snapshots, not reservations or a complete model
context estimate; the server always rechecks admission. Composer and history
show prepared excerpt length/truncation, original/stored bytes and image
dimensions where recorded. Image dimensions also reserve preview space.
Migration 26 adds `reading_scope` without reparsing old attachments: older
records explicitly report unavailable details rather than invented statistics.
Prepared content is not proof of model attention; expiry and context selection
may omit files/history. Existing model-context warnings remain authoritative.

Session scrolling follows streamed text and image/layout growth only while
near the bottom. Scrolling up pauses following and reveals a keyboard-accessible
back-to-bottom control with a new-reply notice. Historical pages remember their
individual reading anchors when navigating between older/latest pages; changing
pages never restores against the previous page's in-flight response.

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
