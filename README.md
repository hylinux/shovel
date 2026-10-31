# Shovel

**Local-first by default, extensible when needed.**

Shovel is a personal AI Agent platform built on **Microsoft Agent Framework
(MAF), FastAPI, and AG-UI**. Its Web workbench combines streaming conversation,
local knowledge retrieval, native long-term Memory, and trusted workspace
extensions. CLI and Web management share one Generic Host and durable work queue.

**SQLite is the source of truth; Zvec indexes are derived and rebuildable.**
The default storage architecture needs no external database, cache, or Memory
Server. Models can run locally through Ollama or use configured remote providers;
local storage does not make remote inference local.

## Current progress

Current implementation as of **2026-10-30**:

| Area | Implemented |
| --- | --- |
| Main Agent | MAF Agent, native tool loop, context providers, session handling, and AG-UI streaming through FastAPI |
| Knowledge / RAG | Local folders and classic Outlook, durable scans, parent/child chunks, SQLite FTS5, Zvec vectors, hybrid ranking, and numbered citations |
| Native Memory | Separate history/summaries/facts, structured extraction, deduplication/consolidation, supersession, expiry, soft/hard deletion, provenance, scope isolation, bounded recall, and index recovery |
| Workspace extensions | MAF Skills, MCP stdio/HTTP, read-only built-ins, and opt-in Python functions and child Agent/Workflow tools |
| Capability platform | Immutable versions, one durable Execution engine, narrowing permissions, explicit approvals, trigger-agnostic Workflow V1 and interval/daily/event automation |
| Web workbench | Expanded Chat session tree, persistent session rename/delete/history, Search Documents, Resources, Background tasks, and tabbed Settings |
| Settings | Separate runtime, native Memory, MCP management, and editable Agent instructions tabs with automatic Agent restart |
| Reliability | Durable Operations, idempotency receipts, cancellation/restart recovery, vector deletion Outboxes, and foreground-aware background work |
| Distribution | Static Next.js export served by Python in production, bundled wheels, and installed-package smoke checks in CI |

This is a **development-stage, loopback-only, single-owner runtime**, not a
public multi-user service. The Capability platform includes a serial Workflow V1
editor and interval/daily automation. Visual Agent authoring, cron/watch scheduling,
Graph Memory, and automatic Procedural Memory learning are not implemented.
Workspace Agent/Workflow tools are supported; they are not a complete multi-Agent
management product. See the [long-term designs](#documentation) for future goals,
not the current completion checklist.

## Contents

- [Run the local workbench](#run-the-local-workbench)
- [First knowledge loop](#first-knowledge-loop-without-model-credentials)
- [Configure chat and semantic retrieval](#configure-chat-and-semantic-retrieval)
- [Main Agent and workspace extensions](#main-agent-and-workspace-extensions)
- [Native Memory](#native-memory)
- [REST surface](#rest-surface)
- [Reliable submissions and profiles](#reliable-submissions-and-profile-management)
- [History maintenance and quality baselines](#explicit-history-maintenance-and-quality-baselines)
- [Development and validation](#development-and-validation)
- [Documentation](#documentation)

## Run the local workbench

Development requires Python 3.14+, Git, and Node.js 22.18+ (22 LTS in CI).
`py-generic-host` is pinned to a Git commit in both source and wheel dependencies;
no sibling checkout is required. On Windows, use **64-bit x64 Python** with the
committed Zvec 0.7 dependency; it can also run under x64 emulation on Windows ARM64.
Select it explicitly with `uv sync --python 3.14-x86_64 --locked --all-extras --dev`.
The current lock does not include a native Windows ARM64 Zvec wheel. Do not mix
Windows and WSL virtual environments or source paths.

```powershell
uv sync --locked --all-extras --dev
uv run shovel init
Set-Location frontend
npm ci
Set-Location ..
uv run shovel run dev
```

`shovel init` prints the configuration path, normally
`%USERPROFILE%\.shovel\config\settings.toml` on Windows. The workbench and keyword
indexing can run without model credentials; configure models before using chat
or semantic retrieval. A complete local Ollama configuration is available in
[Documents/settings.example.toml](Documents/settings.example.toml).
Adjust its Windows paths and installed model names before using it.

`run dev` starts **two separate services**: Next.js on `http://127.0.0.1:3000`
and the Generic Host/FastAPI backend on `http://127.0.0.1:9999`. Next.js proxies
`/api/*` to the backend, so the browser uses same-origin REST and AG-UI requests. Top-level
configuration keys `dev_frontend_port`, `dev_http_host`, and `dev_http_port` control
these addresses. Both services stop when either exits or the command is stopped.

The sidebar exposes **Chat** with indented New Session/existing sessions,
**Search Documents**, **Resources**, **Background tasks**, and **Settings**.
The platform panels add **Proactive Services**, **Approval Center**, **Activity**,
**Skills** and **Workflows**.
MCP Server Management lives inside Settings, alongside native Memory and
provider/performance status. Editing model/storage configuration still requires
editing TOML or environment variables and restarting the Host.

<details>
<summary>Development diagnostics and streaming behavior</summary>

`run dev` also prints detailed backend diagnostics to the terminal: HTTP route,
status and duration, operation/job IDs, scan stages and counts, retrieval, model
calls, and Memory actions. Responses include `X-Shovel-Request-ID` for correlating
requests with these logs. Backend diagnostics omit request/response bodies,
credentials, document text and source paths. Successful routine polling reads
(status, resource/job/operation lists, scan profiles and job events) omit HTTP
start/completion logs unless they take at least one second. Failed responses,
exceptions and slow requests remain visible, as do scan, Agent and Memory
diagnostics. Next.js omits duplicate access logs for these polling URLs; the
backend remains responsible for their failure and latency diagnostics.
Chat submissions additionally carry one AG-UI `run_id` from the browser through
the backend. `[shovel.chat]` browser logs show submission, HTTP status/request ID,
first text, tool activity, persistence confirmation and terminal outcome.
Backend logs share that `run_id` and report history/persistence, extensions,
context/RAG, individual model calls, tools, Memory recall, background extraction,
session summaries, and index updates, including counts and timings. SSE heartbeats show
that a quiet request is still open; they do not mean the model has completed.
`model.call.*` timings cover individual model calls; the existing `model.completed`
duration covers the whole Agent run, including context and Memory work.
Logs include metadata only, never prompts, answers, retrieved excerpts, Memory
facts, tool arguments/results or credentials.
AG-UI responses use `Cache-Control: no-cache, no-transform` so the Next.js
development proxy does not gzip-buffer small SSE events until the run ends.
Other responses retain their normal compression behavior.
Next.js server output and browser
console logs are forwarded with the `web` prefix; browser console output is
forwarded verbatim, so do not log sensitive data there. These additional
diagnostics are scoped to `run dev`, not `run prd` or other CLI commands.
Normal production lifecycle and error logs are unchanged.

</details>

### Production

Production uses **only Python/FastAPI**, serving an already-exported frontend:

```powershell
Set-Location frontend
npm run build
Set-Location ..
uv run shovel run prd
```

Open `http://127.0.0.1:8080`. The production entry point never starts Node.js.
Static assets are resolved from `--frontend-dir`, the top-level `frontend_path`
configuration, bundled package assets, or the source checkout's `frontend\out`,
in that order. Release wheels bundle the frontend. A source-only Python build
must be given exported assets explicitly; missing assets cause a startup error.
The installed Host framework has no `app.frontend()` method, so Shovel provides
the equivalent `mount_frontend` adapter using FastAPI/Starlette static hosting.

Only loopback listeners are supported. Host/Origin checks prevent ordinary
cross-site browser access; this is not an authenticated, public, multi-user API.
Stop the running Host before reinitializing stores or changing storage settings.

## First knowledge loop, without model credentials

Use the Resources page to add a local directory, verify it, then scan with
embedding disabled. Markdown, text, PDF text layers, Word, Excel, and PowerPoint
are supported; scanned-image PDF OCR is not implemented. Knowledge chat includes
a standalone search view. Choose keyword retrieval to search the indexed text
without an LLM, embedding server, or GPU.

The same operation is available through the CLI:

```powershell
uv run shovel resource add notes --kind local_fs --set 'root_path=C:\Notes'
uv run shovel scan run notes --no-embed
uv run shovel scan list
```

### Index recovery and cleanup

After configuring embedding, run `shovel scan run <resource>` to finish documents
previously scanned with `--no-embed` or interrupted during vector persistence.
Pending vectors and changed chunker/model versions also trigger reprocessing.
Use `shovel scan run <resource> --mode full_sweep --force` to rebuild all discovered
documents after an externally lost knowledge index; normal source filters still apply.
Memory has its own independent [rebuild and recovery flow](#consistency-recovery-and-management).

`shovel resource remove <resource>` persists a deletion intent before cleaning its
vectors. If cleanup fails, retry with `shovel scan cleanup`, optionally passing
`--resource-id <id>`. Cleanup does not require an embedding service.

Chunker v2 isolates chunk IDs by resource. Existing documents are reindexed on their
next scan, which incurs embedding work. See [the pipeline documentation](Documents/scan_pipeline.md)
for the recovery protocol and its limits.

### Local Outlook mail on Windows

Install the optional integration with `uv sync --locked --extra outlook` (or
`--all-extras`), open **classic Outlook** with your normal profile, and run Shovel
in the same logged-in Windows user session. New Outlook, unattended Windows
services, and direct parsing of raw PST/OST files are not supported. PST archives
already mounted in classic Outlook can be selected like other stores.

In Resources, choose **Classic Outlook mail (Windows)**, select a mailbox and
explicit folders, then add/verify and scan. Subfolders are opt-in. The same
resource works through the existing CLI:

```powershell
uv run shovel resource outlook-stores
uv run shovel resource outlook-folders --store-id "<StoreID>"
# Add --parent-id "<FolderID>" to browse children.
uv run shovel resource add mail --kind outlook_local --config-json '{"store_id":"<StoreID>","folder_ids":["<FolderID>"],"include_subfolders":false}'
uv run shovel resource verify mail
uv run shovel scan run mail --no-embed
```

Mail headers and readable cached text bodies are indexed, including searchable
sender/recipient context and source metadata. Attachment names/sizes are recorded,
not attachment contents. No mail is sent, moved, deleted, marked read, or saved.
Outlook permissions/security prompts are not bypassed. Header-only, empty, changed,
or unreadable messages produce visible failures; download full messages in Outlook
and retry. Outlook/provider activity may still involve network access.

**Deleting mail in Outlook does not delete its indexed Shovel copy.** Automatic
deletion detection is deliberately disabled, including full sweeps. Remove the
Shovel resource and clean its index to erase its indexed contents; this never
deletes original mail. Remote embedding/chat providers receive mail excerpts when
used. See [Outlook integration](Documents/outlook_local.md) for identity, filtering,
worker isolation and testing details.

All resource mutations, scans, and cleanup use the same Host-owned background
queue. With a running backend, the CLI submits REST requests and waits for a
durable operation receipt. Otherwise it starts a short-lived, non-Web Host.
Read-only CLI queries use SQLite directly. OS locks protect the database and
vector directory from competing runtime writers.

Operations and scan parameters survive restart. Interrupted work resumes with the
same Job ID; cancellation, partial failure, retries, discovery progress, and job
events are visible in Background tasks. Deleting a resource keeps its operation
receipt and durable vector deletion intents even after its metadata is gone.

## Configure chat and semantic retrieval

Edit the TOML file printed by `shovel init`, or set nested environment variables,
for example `SHOVEL_MODEL__SECURITY_KEY`. Merge the following into the existing
configuration rather than duplicating table headings. Restart the Host after
changing configuration.

### Local Ollama example

The repository includes a complete
[settings.example.toml](Documents/settings.example.toml) and a
[shovel-chat-fast.Modelfile](Documents/shovel-chat-fast.Modelfile).
With Ollama installed and its service running, execute from the repository root:

```powershell
ollama pull qwen2.5:3b
ollama pull bge-m3
ollama create shovel-chat-fast -f .\Documents\shovel-chat-fast.Modelfile
ollama show shovel-chat-fast
```

These are explicit download/create commands, not actions performed by Shovel.
The Modelfile sets an 8192-token context and temperature 0.1 on `qwen2.5:3b`;
creating the model does not train or fine-tune its weights.
The example uses this model for chat and Memory extraction, with `bge-m3`
configured independently for knowledge and Memory embeddings.

The chat portion is:

```toml
[model]
model_provider_type = "ollama"
base_endpoint = "http://127.0.0.1:11434"
model_name = "shovel-chat-fast"
max_input_chars = 4000
max_output_tokens = 512
timeout_seconds = 300
```

Shovel does not install Ollama, download models, or manage GPU drivers. The
character budget bounds history, retrieved evidence, and recalled Memory;
it is not a tokenizer-based context-window guarantee. Lower it for small models.
MAF uses Chat Completions with provider-adapted output limits; Ollama also receives
`max_tokens` explicitly. Models requiring a different API need a matching adapter.
If an Ollama Agent response ends empty without any actionable content or tool
calls, Shovel attempts one tool-free answer recovery using the same context.
The saved answer includes a recovery warning; existing tool calls are not replayed.
If recovery also produces no text, the run remains failed and no Memory extraction
is queued. Truncated responses are not retried through this recovery path.

### Other providers and retrieval

Supported chat adapters include OpenAI, DeepSeek, Qianwen, Kimi, Ollama, and Azure
OpenAI-compatible deployments. OpenAI-compatible `base_endpoint` includes `/v1`
where required; Ollama uses the server root. Azure uses the resource endpoint,
deployment name in `model_name`, and `api_version`. `azure_foundry` currently means
this same deployment API, not the Foundry hosted-agent protocol. The
`microsoft_copilot` provider requires an explicit compatible endpoint and credential;
it does not provide a Copilot subscription login flow.

Knowledge embeddings are configured separately under `[embedding]`, not inferred
from the chat model. Its `provider` can be `openai`, `azure_openai`, or `ollama`;
set `model`, `base_url`, and any required `api_key`/`api_version`. Its output
dimension must match `zvec.dim`. Initialize the vector collection with
`shovel init` before vector scans/searches. Changing dimension requires a new
knowledge-store path and reindexing, not editing the dimension of an existing store.

`auto` retrieval uses hybrid search when embeddings are configured, otherwise
keyword search. `keyword`, `vector`, and `hybrid` can also be selected explicitly.
Hybrid uses reciprocal-rank fusion; SQLite remains authoritative for resource
visibility, document revision, and citation text. Revoked/deleted resources are
excluded. Pausing stops scanning but does not hide previously indexed knowledge.

Chat persists both turns and actual referenced source excerpts. An invalid
source number fails the answer explicitly; absent evidence or omitted citations
produce a warning. Number validation does not prove that every model claim is
supported: inspect the citation excerpts. Remote chat/embedding providers receive
the question and relevant excerpts; use local models for fully local processing.
The Web chat streams AG-UI events through `POST /api/v1/agent`, on the existing
FastAPI port (9999 in development, 8080 in production). There is no separate
Agent listener on `agent_port`. The existing message POST remains a non-streaming
compatibility endpoint, backed by the same MAF Agent.

## Main Agent and workspace extensions

The main Agent uses MAF's `Agent`, Chat Completions client, tool-calling loop,
`AgentSession`, context-provider hooks, native sliding-window compaction, and
`SkillsProvider`. A Shovel context provider supplies bounded SQLite conversation
history, existing RAG evidence, session summaries, and bounded native Memory
recall. Its after-run hook validates citations; successful answers queue durable
user-only fact extraction. SQLite retains turns, cited excerpts, and tool audit
metadata, not complete tool payloads. Loaded Skill instructions remain run-local.
This is not a custom HTTP model execution loop.
`model.max_input_chars` bounds text context, not tool schemas or exact tokens.
Oversized Skill/tool context fails explicitly instead of silently exceeding it.

By default, the workspace is `%USERPROFILE%\.shovel\workspaces\default` on
Windows. `shovel init` creates `skills`, `functions`, and `pipeline` below the
configured workspace. Agent extensions are loaded lazily on the first chat.
Use **Settings → MCP Server Management → Restart Agent** after changing extensions or
`mcp.json`, or restart the Host.

Settings has four tabs: **Runtime configuration**, **Manage long-term Memory**,
**MCP Server Management**, and **Agent instructions**. Switching tabs preserves
mounted editors and unsaved drafts.

**Agent instructions** saves additional role, tone, and behavior instructions in
the workspace's `agent-instructions.md`. Shovel retains its built-in citation,
Memory, and tool-use rules and appends the custom text to MAF's `instructions`.
Saving atomically writes the UTF-8 file with revision conflict protection and
automatically replaces the main Agent. The next message, including in an existing
session, uses the new instructions; conversations, Memory, and background tasks
are retained. Model and extension clients still initialize on demand.
Saving is rejected while any conversation or model warmup is active, without
writing the file or interrupting the run. Empty instructions restore the built-in
defaults. Custom text counts toward `model.max_input_chars`; the editor exposes
the available character limit. Native Memory extraction and summary prompts are
not changed. If saving succeeds but restart fails, reload the saved file and retry
**Restart Agent**. Do not store credentials in instructions.

```toml
[agent]
workspace_path = '~\.shovel\workspaces\default'
enable_python_extensions = false
max_tool_iterations = 10
```

The built-in tools are read-only: `search` uses the existing RAG and respects the
request's resource selection/retrieval mode, `list_resources` lists resources, and
`list_extensions` reports configured extension directories. Tool searches share
the current answer's numbered citation registry. Resource writes, scans, and
deletions remain in the existing management interfaces, not Agent built-ins.

**Skills:** put each skill in `skills\<name>\SKILL.md`, with the standard YAML
`name` and `description` frontmatter. MAF advertises metadata and loads the body
and reference files on demand using its native tools; it does not concatenate
every Skill into the system prompt. Python files in a skill's `scripts` directory
can run only when `enable_python_extensions = true`, with string-array arguments
and a 30-second execution timeout.

**Functions:** when Python extensions are explicitly enabled, each top-level
`functions\*.py` module exports a `tools` list of MAF `@tool` functions or typed
Python callables with descriptive docstrings. For example:

```python
from agent_framework import tool


@tool
def double(value: int) -> int:
    """Double an integer."""
    return value * 2


tools = [double]
```

**Agents/workflows:** each top-level `pipeline\*.py` module exports
`create_agent(client)` (sync or async). It receives the configured MAF chat client
and returns a MAF `Agent` or `Workflow`. For example:

```python
from agent_framework import Agent


def create_agent(client):
    return Agent(
        client=client,
        name="editor",
        description="Review and improve the supplied text.",
        instructions="Improve the supplied text without inventing facts.",
    )
```

The main Agent can invoke it as `pipeline_<module_name>` through MAF's native
`as_tool()` support. A Workflow must be compatible with MAF's `as_agent()`:
its start executor accepts `list[Message]`. Child Agents configure their own
instructions, tools, context providers, and lifecycle; they do not automatically
inherit the main Agent's RAG/Memory context.

**MCP:** create `mcp.json` at the workspace root with a `mcpServers` object.
Supported transports are stdio (`command`/`args`/`env`) and Streamable HTTP
(`url`/`headers`). Each entry optionally accepts `type` (`stdio` or `http`),
`disabled`, and `allowed_tools`. For example:

```json
{
  "mcpServers": {
    "documents": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${DOCUMENTS_MCP_TOKEN}"},
      "allowed_tools": ["search"]
    }
  }
}
```

`${NAME}` references an environment variable; a missing reference is an explicit
configuration error. MCP tools are namespaced by server. Shovel uses MAF's native
MCP connections and closes them with the Runtime. Invalid extension/MCP
configuration fails chat explicitly rather than silently dropping capabilities.

The Web **Settings → MCP Server Management** section lists saved servers and provides
a JSON editor for the current workspace's `mcp.json`. Saving validates the schema
and atomically replaces the file, rejecting stale editor revisions rather than
overwriting another edit. Invalid existing JSON remains editable. Saving does not
launch commands, connect servers or change already-cached tools. **Restart Agent**
closes cached MCP/model clients; connections and extensions are loaded again on
the next chat. It is rejected while any session is generating and preserves
conversation history, Memory and background tasks. Unresolved environment
references can be saved but fail explicitly when an enabled server loads.
Changing Host environment variables still requires restarting the Host.
The editor displays the actual configuration, including any inline credentials;
prefer environment references and do not share screenshots containing secrets.

**Trust boundary:** enabling Python extensions grants the local modules and
Skill scripts the Shovel process user's permissions. Configuring a stdio MCP
server authorizes launching that command; remote MCP servers receive tool
arguments. These are trusted extensions, not sandboxed code or read-only tools.
Review them and restrict MCP tool lists before enabling them. Shovel does not
install user extensions or dependencies automatically.

### Conversation performance

The Web Host warms its existing Zvec handle in the background. When a provider
is Ollama on an HTTP loopback endpoint, it also warms configured Memory/RAG
embedders and the chat model using synthetic input (one output token for chat).
It never loads Skills/MCP/Python extensions, saves turns or extracts facts during
warmup. Cloud and remote model endpoints and Ollama models ending in `-cloud`
or `:cloud` are not automatically called. A loopback Ollama server can still
proxy other model aliases to a cloud service; disable `warm_local_models` if
local execution cannot be guaranteed.
Short-lived CLI Hosts retain on-demand initialization.

Warmup waits for foreground idle between components and reports `cold`, `warming`,
`ready`, `failed`, `cancelled` or `disabled` through `/api/v1/status` and Settings.
A failure is logged by component/error type, without credentials or paths; it
does not silently replace vector search with keyword search or create an index.
Ready means warmup completed, not a guarantee that Ollama will retain all models
in memory. A chat submitted before warmup completes can still pay startup cost.

```toml
[performance]
warm_index = true
warm_local_models = true
max_foreground_runs = 0
max_local_model_requests = 1
foreground_queue_seconds = 120
background_idle_seconds = 5
embedding_cache_entries = 128
embedding_cache_seconds = 300
context_profile = "default" # opt in to "compact" for bounded CPU-oriented context
```

`max_foreground_runs = 0` selects one concurrent foreground run for Ollama and
four for other chat providers. Positive values override that limit. Admission
covers context preparation through durable answer persistence; queued requests
count as foreground activity and can be cancelled without leaking capacity.
Queue timeout fails explicitly rather than allowing unlimited simultaneous
model work. Memory and RAG reads within an admitted run execute concurrently,
then use the existing deterministic context budget and citation validation.

The extraction worker remains single-concurrency/FIFO and starts tasks only
after all foreground runs finish and the idle window expires. It checks again
before inference. Already-running extraction/warmup is not preempted: cancelling
a client process does not guarantee that a model server stops computing.
Local Ollama generation requests from Main Agent, platform Skill/Agent, Memory
and model warmup share `max_local_model_requests` slots. The lease covers the
actual HTTP response body, not an entire Agent run: a parent releases its slot
before invoking a child model tool. Interactive requests precede queued platform,
Memory and warmup requests; requests waiting at least 30 seconds receive FIFO
priority to prevent starvation. The queue holds at most 64 waiting requests and
uses `foreground_queue_seconds` as its timeout. Already-running inference is not
preempted, so a foreground request can wait for a background call to finish.
Remote/cloud providers are excluded. Query embedding reads remain independently
parallel rather than being unconditionally serialized with generation.
Chat model warmup precedes index and embedding warmup; the index still opens
once, safely off the event loop, and Agent retrieval can await that same handle.

Every Session uses the full **Agent**, with automatic document retrieval, tools,
workspace extensions, Memory, history and summaries. There is no Agent/Chat
mode selector or `interaction_mode` request field. Existing permission and
approval boundaries still apply; enabling tools never grants permissions.

`context_profile = "compact"` is opt-in. It caps serialized source allocation at
2500 characters, Memory at 1000, summary at 1000, and complete recent user-led
history groups at 4000, subject to the existing total budget. Instructions are
retained, no synchronous summary model is introduced, and stored history is not
deleted. Compact mode also reserves serialized tool-schema space and checks it
at the final call; if tools exceed the budget, it fails explicitly rather than
dropping tools. Use the default profile or a larger character budget.
Smaller budgets can reduce continuity or evidence detail: validate
quality before enabling them. The default profile and retrieval candidate count
remain unchanged. RAG metadata is read in batches, and concurrent identical
embedding misses share bounded work with independent cancellation; retrieval
results and permission decisions are never cached.

Development logs separately time scope/keyword/embedding/index/search,
SQLite revalidation, metadata hydration, persistence and summary/status reads.
Model diagnostics distinguish message characters from serialized tool-schema
size and provider token usage, including instructions carried separately in MAF
options. The final character guard includes those instructions; they do not log
prompts, sources or credentials.
Event-loop delay is sampled only during development chats.

#### CPU-only deployment and measurement

Use `run prd` with the existing static frontend to avoid Next.js development/HMR
overhead during normal desktop use. Do not assume a GPU or automatically change
models. As starting candidates, an 8 GB machine may need a roughly 1–3B quantized
model; 16 GB can compare small models against roughly 3–8B quantized candidates;
32 GB offers more residency headroom but does not guarantee faster generation.
Measure RAM pressure and Chinese/tool/structured-output quality, not just model
names. Smaller context and one generation slot can avoid oversubscription.
Ollama thread/context/residency options belong to supported Ollama configuration,
not invented `[model]` keys. Shovel neither installs models nor changes OS settings.

`[memory.llm]` already defaults to `[model]`, which can reduce duplicate model
residency; a separate small model is optional and competes for the same CPU/RAM.
Changing knowledge or Memory embeddings requires rebuilding that respective
index with the matching model/dimension, never mixing vector versions in place.

The optional local comparison uses an isolated SQLite/workspace/index and the
committed test corpus, rejects remote/cloud model providers, and never loads
your workspace extensions. It explicitly disables Memory per benchmark request
to isolate model/context/RAG costs; Memory and background contention are covered
by offline correctness tests, not measured by this comparison. It reports
backend first-text and durable-completion P50/P95 and output size for the full
Agent with default/compact profiles. Small samples are exploratory, not a speed or
quality guarantee; compare citations and answers separately, and use the existing
live knowledge baseline when appropriate.

```powershell
$env:SHOVEL_RUN_CPU_BENCHMARK = "1"
$env:SHOVEL_CPU_CONFIG = "C:\path\to\settings.toml"
$env:SHOVEL_CPU_SAMPLES = "3"
uv run pytest backend\tests\test_cpu_performance.py -m live --no-cov -s
```

Only successful, valid embedding vectors are cached in bounded, expiring,
process-local caches. Memory and RAG caches are separate; both use keys derived
from query text and model version. No prompt text is retained as a cache key or
logged. Search results,
permissions, revision checks and Memory facts are not cached: deletion/forget
and resource visibility still take effect. Set entries to zero to disable.
The RAG HTTP client is owned/closed by Runtime rather than recreated per query;
the background extractor owns and reuses a separate async MAF model client.

Development logs split index thread scheduling/import/runtime/collection-open,
query embedding, chat admission, first model text and completion. Model logs
include provider/model and token usage when supplied; provider-native durations
are recorded only if exposed by the compatible API. `first_text_ms` includes
provider waiting/loading/prompt work and must not be called pure prefill time.
The existing outer `model.completed` includes context/hooks; use
`model.call.completed` for the individual model request. No speedup is promised
without a cold/warm benchmark on the configured model and hardware.

## Native Memory

SQLite is the source of truth. Zvec is a separate, rebuildable semantic index;
the shared Embedder produces its vectors. No external database, cache service,
Memory Framework, or separate Memory Server is required.

Conversation History, Session Summary and long-term Memory are distinct:

- SQLite stores original user/assistant turns and necessary tool audit metadata.
- Background summaries compress older completed turns and retain a sequence watermark.
  Summaries are session context, not extracted user facts or a vectorized message copy.
  Until background summarization catches up, uncovered older turns are not in Agent context.
- Semantic facts and episodic outcomes are extracted only from direct user assertions.
  Procedural records can be explicitly maintained; automatic procedural learning is disabled.

The existing Generic Host Memory worker runs durable `memory.extract` and
`memory.summarize` Operations after foreground idle. Answer persistence and task
enqueue share one SQLite transaction. Structured MAF extraction has no tools and
cannot directly mutate storage. Evidence, confidence and source attribution are
validated before consolidation. Exact/canonical duplicates merge provenance;
semantic matches require an explicit equivalence decision, not similarity alone.
Explicit corrections supersede earlier facts without overwriting their history.
Unsupported, low-confidence, credential-containing and ambiguously quoted inputs
are conservatively excluded. These controls are not a guarantee against every
form of prompt injection; configure trusted models and review saved facts.

Core user attributes (name, occupation, timezone, language) are read directly
from visible, active, unexpired SQLite facts, independently of vector similarity.
Recognized pure profile questions display committed values deterministically,
including streaming and persisted answers, without asking the model to guess.
Profiles use source assertion times, including reaffirmed duplicate facts, rather
than delayed extraction completion times. Recent uncommitted session assertions
retain the history path; newer committed updates from other Sessions take precedence.
This is not a separate Session mode: mixed requests and explicit document scopes
still use the full Agent and its tools.

For other requests, active facts are recalled before the main Agent call,
alongside independent RAG, with Memory separated from document reference data.
Vector results must pass SQLite ownership, lifecycle, revision, embedding-version
and expiry checks. Relevance is a gate before importance, confidence, recency and
access-history ranking. Only selected facts enter the configured character/item
budget. Current user input takes precedence over historical Memory. Memory never
becomes a local-document citation.

Workspace and user are mandatory partitions; agent and session scopes further
restrict visibility. V1 retains the loopback-only, single local owner
(`local-user`), not a multi-user authentication server. Scope IDs come from the
Host, not extraction output or arbitrary browser ownership fields.

### Configuration

Memory works locally in SQLite without a configured embedder. Chat remains
available, but explicitly warns that semantic recall is unavailable. Configure
an independent Memory embedding model to enable vector recall:

```toml
[memory]
dim = 1024
context_max_chars = 2000
max_items = 10
min_similarity = 0.55
min_confidence = 0.8
history_messages = 20
summary_max_chars = 2000
# index_path defaults to ~/.shovel/memory/zvec

[memory.embedder]
provider = "azure_openai"
model = "YOUR_EMBEDDING_DEPLOYMENT"
base_url = "https://YOUR_RESOURCE.openai.azure.com"
api_version = "2024-02-01"
# Set SHOVEL_MEMORY__EMBEDDER__API_KEY through the environment.
```

`memory.dim` must match the embedding deployment's actual output dimension.
The Memory and knowledge embedding configurations are independent. The input
budget is in **characters, not tokens**, including serialized Memory metadata.

Extraction and summaries follow `[model]` by default. To use a dedicated model,
`[memory.llm]` uses the same keys as `[model]`, including Azure `api_version`:

```toml
[memory.llm]
model_provider_type = "ollama"
model_name = "YOUR_INSTALLED_STRUCTURED_OUTPUT_MODEL"
base_endpoint = "http://127.0.0.1:11434"
max_output_tokens = 2048
timeout_seconds = 120
```

No model is installed or downloaded automatically. Local-first storage does not
make cloud inference local: configured cloud embedders/extractors receive the
relevant user text. Cancelling a request does not guarantee server-side inference
stops. Recognized standalone greetings skip automatic RAG and fact extraction;
normal SQLite history still persists.

### Consistency, recovery and management

Facts, supersession, index Outbox entries and extraction receipts commit
atomically in SQLite. An indexing failure does not undo a saved fact. The worker
retries persisted upserts/deletions with bounded backoff and acknowledges them
only after Zvec flushes. Index status and pending counts are exposed in Settings.
Extraction errors are retried up to three attempts and remain visible in
Background tasks. The extraction receipt and saved facts cannot commit separately.

Changing the embedding identity/dimension or losing an index queues a rebuild
from SQLite. Explicit rebuilds write a new generation and switch after pending
mutations have drained; previous derived directories are not automatically
deleted. Rebuild uses the configured embedding service and may incur its cost.

Settings loads saved facts only on request, supports confirmed soft deletion
and semantic-index rebuild. Soft-deleted fingerprints prevent automatic
relearning from older assertions. Forgetting invalidates older pending Memory
tasks only in this workspace/user partition and fences late extraction writes.
Session deletion removes its summary and session-scoped Memory, not cross-session
facts. Neither operation erases source messages in other sessions or backups.

`GET /api/v1/memories?include_history=true` includes lifecycle history.
`POST /memories`, `PUT /memories/{id}`, and `POST /memory/index/rebuild` under
`/api/v1` return durable HTTP 202 Operations and support `Idempotency-Key`.
Explicit management supports semantic, episodic and procedural records,
`expires_at`, and user/workspace/agent/session scope (`session_id` is required
only for session scope). `DELETE /memories/{id}?hard=true` removes the SQLite
record while retaining its vector-cleanup intent; normal deletion is soft.

## REST surface

The live API reference is `/docs`, with schema at `/openapi.json`.
Routes are under `/api/v1`; collection responses use `{"items": [...]}`.
Durable resource/scan/cleanup and Memory create/update/rebuild requests return
**202 + an Operation**, not the completed result.
Poll `/operations/{id}` for completion; normal Memory deletion returns
`{"deleted": true}` instead.

| Surface | Endpoints |
| --- | --- |
| Runtime | `GET /status`, `GET /connectors` |
| Local Outlook | `GET /outlook/stores`, `GET /outlook/folders?store_id=...&parent_id=...` |
| Resources | `GET/POST /resources`, `GET/PATCH/DELETE /resources/{id}` |
| Resource actions | `POST /resources/{id}/verify`, `/pause`, `/resume`, `/revoke`, `/scans` |
| Profiles | `GET /resources/{id}/profiles`, `PUT/DELETE /resources/{id}/profiles/{name}` |
| Named commands | `POST /commands` carries `kind`, `ref`, `payload` without putting resource names in URL paths |
| Work | `GET /operations`, `GET /operations/{id}`, `POST /operations/{id}/retry` |
| Scans | `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/events`, `POST /jobs/{id}/cancel` |
| Index | `GET /index/cleanup`, `POST /index/cleanup`, `POST /search` |
| Chat | `GET/POST /conversations`, `PATCH/DELETE /conversations/{id}`, `GET/POST /conversations/{id}/messages` |
| Agent | `POST /agent` accepts AG-UI `RunAgentInput` and streams SSE |
| MCP management | `GET/PUT /agent/mcp`, `POST /agent/restart` |
| Agent instructions | `GET/PUT /agent/instructions` (workspace text, revision, built-in policies, budget, and application state; PUT saves and restarts) |
| History | `POST /history/prune` explicitly prunes bounded, old terminal history |
| Memory | `GET/POST /memories`, `PUT/DELETE /memories/{id}` |
| Memory index | `POST /memory/index/rebuild` |
| Credentials | `POST /credentials` writes the OS keyring and returns an `identity_ref` |

Source text and paths are shown as citation data; there is no API for opening an
arbitrary filesystem path. Raw resource credentials must not appear in queued
config/payloads; use `identity_ref`. Model credentials remain in local
configuration/environment variables and should not be committed.

### Capability and Execution entry points

The Main Agent, HTTP API and new CLI groups share `PlatformApplication` and the
existing Capability, Invocation, Approval, Workflow and Proactive application
services. New entry points never access ORM objects. Existing resource, scan and
chat entry-point persistence remains legacy code; this is not a claim of global
ORM-boundary convergence.

Use `GET /api/v1/platform/capabilities`, `/executions`, `/proactive`, `/approvals`
and `/interactions` to inspect state. `POST /api/v1/platform/commands` accepts a
typed `PlatformCommand`; `/docs` describes its actions and fields. Invocation,
manual service run and scheduled resume return **202 + the same Execution**.
Pass a stable `request_key` (or `Idempotency-Key`) for invocations and manual runs.

Publishing, enabling/resuming automation, approving/rejecting and providing input
require two explicit requests: send the command with `"prepare": true`, review
the returned preview, then resend that exact command with its
`"confirmation_id"` and without `prepare`. Tickets are single-use, expire after
five minutes, and become invalid when the relevant content/revision changes or
the Host restarts. Approval/input commands require both `identifier` (the request
ID) and `execution_id`. These confirmation tickets do not grant permissions.
The Main Agent cannot issue or consume tickets: it only returns a structured
`requires_confirmation` action for the user. Draft services are always disabled.
Pause/disable stops future admissions, not already-created Executions.

```powershell
uv run shovel capability list
uv run shovel capability search "research"
uv run shovel skill versions <capability-id>
uv run shovel workflow validate <capability-id> 1
uv run shovel workflow publish <capability-id> 1
uv run shovel --request-key my-workflow-run workflow run <capability-id> 1
uv run shovel proactive explain <service-id>
uv run shovel approval approve <approval-id> --execution-id <execution-id>
uv run shovel execution show <execution-id>
uv run shovel interaction input <interaction-id> --execution-id <execution-id> --response '"answer"'
```

CLI confirmation displays structured IDs, revision and content before prompting;
there is no implicit publish/approval `--yes`. With a matching Web Host, commands
use HTTP. Otherwise these new commands reuse the short-lived Generic Host, not
a second Runtime; offline run waits for completion or suspension. Console
notifications can accompany CLI output. `proactive explain` reports the latest
durable Execution snapshot and current admission blockers; skipped events are
not durable history.

Permissions default to empty. Configure explicit exact permission pairs locally,
then restart the Host; no API or Agent tool writes this authority:

```toml
[platform]
user_id = "owner"
agent_permissions = [{ action = "read", resource = "knowledge" }]

[platform.security]
user_grants = [{ action = "read", resource = "knowledge" }]
platform_constraints = [{ action = "read", resource = "knowledge" }]
allowed_side_effects = ["none"]
preference = "MINIMUM_REQUIRED"
```

Effective permissions intersect grants, platform constraints, the caller's
ceiling and all Capability/parent boundaries. `agent_permissions` further narrows
Main Agent invocation; permission pairs are exact, not wildcard grants. No real
external-write adapter is enabled by these entry points.

The existing workbench includes **Proactive Services**, **Approval Center**,
**Activity**, **Skills** and **Workflows**. These panels use the same platform API
and confirmation previews as the CLI; the browser never grants permissions.
`GET /api/v1/platform/options` supplies currently authorized published dependency
options, and save/publish recheck the selection on the server.

Service editing saves a disabled new revision; enabling it requires another
explicit review. Per-service pause preserves its enabled flag. Global pause is
persisted separately in workspace/user-scoped application settings: it blocks
automatic Event/Schedule admission, preserves individual states, and allows
manual Run Now. Global resume rechecks enabled targets and does not replay
skipped events or missed schedule occurrences. Existing Executions, approvals
and Execution timers continue while automation is paused.

Skill Memory Access is read-only **Disabled** in V1: isolated tool-free Skills
do not inherit Main Agent Memory or tools. Editors save drafts, validate them,
and publish immutable versions; tests run only published versions through the
normal durable Execution path and may call a configured model provider.
The Workflow V1 designer provides the nine supported serial-DAG nodes and
editable edges, schemas and declarative JSON mappings, not arbitrary scripts,
external trigger nodes, parallelism or loops. Activity can reconcile interrupted
browser runs using their original persisted request keys before resending.

UI/API support preference and quiet-hours editing plus global automation control;
current CLI/Main Agent tools expose only their documented individual-service and
inspection actions, not every UI edit/global command. See the
[implemented architecture](Documents/platform_architecture.md),
[fifteen ADRs](Documents/platform_adrs.md) and
[Phase 13 validation report](Documents/platform_validation.md). The report records
five passing deterministic demos, real browser/package checks, and the remaining
MCP dependency and whole-repository formatting baseline failures; it is not an
all-green release claim.

## Reliable submissions and profile management

All durable writes accept `Idempotency-Key` (1–128 ASCII letters/digits or
`._:-`). Reusing a key for the same normalized request returns its original
receipt, even after resource/profile deletion; different arguments return 409.
Do not use a new key just because a response was lost. Chat generation and
Memory writes do not promise exactly-once execution.
For AG-UI, `threadId` is an existing conversation ID. `forwardedProps` supplies
`resource_ids`, `search_mode`, and `memory_enabled`. The server trusts only the
latest user text, not client-supplied system messages, history, tools, or state.
`RUN_FINISHED` is emitted only after the answer/citations/warnings are committed;
the preceding `shovel.message` custom event contains the saved Message.
Failures emit `RUN_ERROR`, not a successful terminal event. On disconnect, reload
conversation history before retrying; run IDs are not idempotency receipts.

The CLI records a local receipt before submission and accepts a global key:

```powershell
uv run shovel --request-key notes-scan-001 scan run notes --no-embed
uv run shovel operation requests
uv run shovel operation list
uv run shovel operation show <operation-id>
uv run shovel operation wait <operation-id>
uv run shovel operation retry <operation-id>
```

`wait` reconnects to the existing operation, starting a non-Web Host if needed.
Interrupted browser writes stay in workspace-scoped browser storage; Background
tasks can find their receipt or resend the exact request with its original key.
Resource/profile request bodies may contain local paths; clearing browser site
data removes these local recovery records, not server-side operations.

`shovel profile list notes` lists persisted profiles. Use
`shovel profile save notes text --file profile.json` to replace/upsert one:

```json
{"include":["*.txt"],"exclude":[],"scan_mode":"full_sweep","pipeline":{}}
```

Choose it with `shovel scan run notes --profile text --no-embed`, or the scan
form in Resources. The Web profile editor exposes the same validated JSON.
Supported processing settings are `pipeline.chunker` budgets and
`pipeline.parser.max_chars`. Unknown keys and invalid budgets are rejected.
Queued/running scans block profile mutation. Overlapping profiles must use the
same pipeline: each resource/document has one shared representation, not an
independent index per profile.

Full sweeps reconcile membership only within their profile. A document is
tombstoned only when no other profile owns it. Removing a non-default profile
also removes its scan history and its exclusively owned index entries, never
the source files. Vector cleanup failures retain deletion intents and a
retryable receipt. The default profile can be changed/disabled, not deleted.

Local filesystem discovery always enumerates metadata; timestamps are not
reliable cursors. Same-second edits and newly copied files with old timestamps
are therefore discovered. Root/walk/stat failures fail the job without deletion
reconciliation. Complete discovery plus `DELETE_DETECT` is required for deletion.
Fingerprints are size + nanosecond mtime, not cryptographic content hashes;
deliberately restoring both after editing requires `--force`.

Parser versions, parser/chunker settings and chunker defaults invalidate old
processing automatically. Embedding identity also includes endpoint, API version,
dimension, input limit and optional `embedding.revision`; rotating credentials
does not rebuild the index. Use `revision` when replacing weights behind an
unchanged model/deployment name. Runtime configuration reads environment over
TOML over defaults; `AppSettings.load()` intentionally reads the file only.

## Explicit history maintenance and quality baselines

Conversation messages use `limit` (default 100, max 1000) and `before` sequence
pagination; the response includes `next_before`. The workbench exposes older
messages, conversation paging, renaming and confirmed deletion. Deletion removes
messages, summaries and session-scoped Memory, but retains independently saved
cross-session facts. It is refused during generation.

`shovel history prune --keep-days 30 --limit 1000` asks for confirmation before
pruning old terminal Jobs/events, unkeyed Operations, retrieval logs and completed
cleanup records, bounded per category. Active work, pending cleanup, documents,
conversations and keyed receipts are retained. Background tasks exposes the same
confirmed action. Keyed receipts are deliberately retained to prevent a formerly
used request key from executing again. No automatic retention policy is enabled.

Quality gates run Ruff, mypy, pytest (including actual process termination against
SQLite/Zvec), frontend lint/types/tests/build, and clean installed-wheel startup,
REST/CLI/search and shutdown. Dependency installation uses the committed locks.

`backend/tests/fixtures/knowledge` contains fixed synthetic Chinese evidence.
The offline baseline checks keyword recall@3 and revoked-resource visibility.
The opt-in live baseline checks real hybrid recall, required answer facts,
citations and insufficient-evidence behavior using isolated temporary stores:

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = 'C:\path\to\settings.toml'
$env:SHOVEL_BASELINE_REPORT = 'C:\path\to\baseline.json'
uv run pytest backend\tests\test_knowledge_baseline.py -m live --no-cov
```

Select both chat and embedding providers explicitly before running; external
providers receive the synthetic evidence and may charge for calls. The JSON
report records measured baseline results, not a general model-quality guarantee.
Live results depend on the configured models and deployment; offline mocks are
not proof that a particular local model will extract facts or cite sources reliably.

## Development and validation

The Python package lives under `backend\src\shovel`, with tests in `backend\tests`.
`runtime` assembles the Host, Agent, REST/AG-UI, retrieval and background work;
`memory` contains domain policies, ports, services and SQLite/Zvec/MAF adapters.
`pipeline` owns document ingestion and `vector` the shared Zvec infrastructure.
`frontend` is the static-exportable Next.js workbench.

From the repository root, using the committed `uv.lock`:

```powershell
uv run shovel version --verbose
uv run shovel config --check
uv run ruff check .
uv run ruff format --check .
uv run mypy backend\src
uv run pytest
```

`config --check` validates the active configuration; it requires an existing
configuration file. Offline tests do not require model credentials.
For focused native Memory tests:

```powershell
uv run pytest backend\tests\test_native_memory.py backend\tests\test_memory_tasks.py --no-cov
```

From `frontend`, using the committed `package-lock.json`:

```powershell
Set-Location frontend
npm run lint
npm run typecheck
npm test
npm run build
```

CI also builds a wheel with bundled frontend assets, installs it in a separate
environment, and runs `scripts\smoke_wheel.py` with that environment's Python.
See [CI](.github/workflows/ci.yml) and [release](.github/workflows/release.yml)
for the complete pipelines. Do not use `uv run` for the installed-wheel smoke
check: it must exercise the installed artifact, not the source environment.

## Documentation

| Document | Purpose |
| --- | --- |
| [Development environment](prepare-develop-env.md) | Prerequisites, Windows x64 setup, and local Ollama model creation |
| [Complete Ollama configuration](Documents/settings.example.toml) | Chat, independent knowledge/Memory embeddings, SQLite and Zvec paths |
| [Ollama Modelfile](Documents/shovel-chat-fast.Modelfile) | `shovel-chat-fast` model definition and creation command |
| [Frontend workbench](frontend/README.md) | Navigation, AG-UI behavior, Settings, MCP and frontend development |
| [Capability platform architecture](Documents/platform_architecture.md) | Implemented planes, models, permissions/approval, events/timers, persistence and migration boundaries |
| [Capability platform ADRs](Documents/platform_adrs.md) | Fifteen architecture decisions, alternatives, consequences and compatibility |
| [Phase 13 validation](Documents/platform_validation.md) | Five demos, final invariant matrix, verification results and remaining release blockers |
| [Scan pipeline](Documents/scan_pipeline.md) | Ingestion, lifecycle, durable cleanup, DI and recovery contracts |
| [Outlook integration](Documents/outlook_local.md) | Supported Outlook environment, source identity and indexing limits |
| [Memory vector identity](Documents/db_base_memory_vector_id.md) | Deterministic IDs, lifecycle, authority checks and rebuild semantics |
| [Native Memory implementation](Documents/native_memory_architecture.md) | Detailed Chinese guide to principles, architecture, extraction, recall, transactions, recovery and implementation boundaries |
| [Architecture design](shovel-architecture-design.md) | Architecture guidance and the current native Memory module boundaries |
| [Studio product design](shovel-architecture-design-v2.md) | Long-term product goals, not an implemented-feature checklist |
| [Learning roadmap](shovel-learning-roadmap.md) | Project-oriented learning stages and references |
