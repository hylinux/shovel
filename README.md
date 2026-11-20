<img src="frontend/app/icon.svg" alt="Shovel logo" width="64" height="64">

# Shovel

[Logo design and brand assets](Documents/branding/README.md)

**A local-first Agent workbench, a durable Capability platform, and a
default-deny enterprise security foundation.**

Shovel combines Microsoft Agent Framework (MAF), FastAPI, AG-UI, and a
static-exportable React/Next.js workbench. It supports streaming conversations,
local knowledge retrieval, native long-term Memory, versioned Skills and
Workflows, proactive automation, and opt-in workspace extensions.

SQLite holds authoritative text, state, receipts, and audit records. Separate
Zvec indexes provide rebuildable knowledge and Memory retrieval. The personal
workbench needs no external database, cache, or Memory Server. Models can use
Ollama or configured remote providers; **local storage does not make remote
inference local**.

## Current implementation boundary

| Area | Current state |
| --- | --- |
| Personal workbench | Streaming Chat, persistent sessions, Search Documents, Resources, Background tasks, and Settings are implemented. |
| Knowledge and Memory | Durable concurrent ingestion across resources, keyword/vector retrieval, citations, cross-session facts, index cleanup, and restart recovery are implemented. Model-dependent behavior still requires suitable models. |
| Capability platform | Immutable versions, one durable Execution lifecycle, narrowing permissions, approvals, serial Workflow V1, and event/interval/daily Proactive Services are implemented. |
| Enterprise security | Authentication/IPC controls, information-flow and source-policy gates, external-effect recovery contracts, governance controls, and signed rollout enforcement are implemented. Unsupported or unqualified enterprise paths remain denied. |
| Distribution and evidence | Bundled-wheel smoke tests, Windows native/controlled-installation candidates, inventory checks, and CI provenance workflows are implemented. Candidate artifacts are not organization-approved installations. |
| Enterprise production release | **Not completed.** Actual storage/key authority, organization provisioning, independent live measurements, and approved deployment are still required. |

This remains a **development-stage, loopback-only, single-owner application**,
not an authenticated public multi-user service. Do not expose its ports as a
public or shared API. Running `shovel run prd` selects Python-hosted static
serving; it does **not** approve an enterprise rollout.

## Contents

- [Run the local workbench](#run-the-local-workbench)
- [Configuration and models](#configuration-and-models)
- [Knowledge and RAG](#knowledge-and-rag)
- [Native Memory](#native-memory)
- [Capability, Workflow, and Proactive Services](#capability-workflow-and-proactive-services)
- [Main Agent and workspace extensions](#main-agent-and-workspace-extensions)
- [Enterprise security and controlled deployment](#enterprise-security-and-controlled-deployment)
- [API and recovery contracts](#api-and-recovery-contracts)
- [Development, CI, and packaging](#development-ci-and-packaging)
- [Documentation](#documentation)

## Run the local workbench

### Prerequisites

- Python **3.14+**, uv, and Git.
- Node.js **22.18+** for frontend development/builds; CI uses Node 22.
- On Windows, **x64 Python** for the native Zvec dependency, including x64
  emulation on Windows ARM64. The committed lock does not provide a native
  Windows ARM64 Zvec wheel.

Use the committed `uv.lock` and `frontend\package-lock.json`.
`py-generic-host` is pinned to a Git commit; no sibling checkout is required.
Keep Windows and WSL environments separate.

### Development

From the repository root, in PowerShell:

```powershell
uv sync --python 3.14-x86_64 --locked --all-extras --dev
uv run shovel init
Set-Location frontend
npm ci
Set-Location ..
uv run shovel run dev
```

On platforms with supported native dependencies, use
`uv sync --locked --all-extras --dev` with the appropriate Python interpreter.

`shovel init` first asks for its data root: `SHOVEL_ROOT_PATH` when set, otherwise
the user's `.shovel` directory. Press Enter to keep it, or enter another directory
(no extra `.shovel` is appended). Spaces and non-ASCII directory names are supported.
`SHOVEL_ROOT_PATH` must be absolute; interactive/argument relative paths are resolved
once against the current directory. Invalid roots fail without falling back.

```powershell
uv run shovel init --root-path 'D:\ShovelData'
# Non-interactive setup; use --persist-root only to explicitly save a Windows user variable.
uv run shovel init --root-path 'D:\ShovelData' --no-input
$env:SHOVEL_ROOT_PATH = 'D:\ShovelData'
uv run shovel run prd
```

The root contains `config\settings.toml`, `data`, `workspaces`, `knowledge`,
`memory`, `logs` and `tmp`. All CLI commands use the same root. New profiles write
absolute store paths; existing explicit paths remain authoritative and are not
migrated. Changing the root does not move data, change the development source/
frontend directory, or grant controlled enterprise installation trust.

After successful custom-root initialization, Windows offers an opt-in user-level
environment update. `--persist-root` requests it without prompting. Initialization
failure or cancellation never writes that variable. Persistence does not modify
the parent terminal: use the displayed `$env` command there and open a new terminal
for the saved value. Other platforms display a shell export to add to the shell
profile; automatic `--persist-root` is Windows-only. No machine-level variable is
written. A running Host keeps its original root and actual configuration file.

On first run, `shovel init` then asks for the chat provider (Azure OpenAI, OpenAI,
DeepSeek, Qianwen, Ollama or Microsoft Foundry), endpoint, model/deployment, API version and hidden API key;
then the knowledge name, embedding provider (Azure OpenAI, OpenAI, Qianwen or
Ollama), endpoint, model/deployment, dimensions, hidden API key, API version and
Memory collection name. Blank keys are allowed; Azure OpenAI/Foundry blank keys select
non-interactive Azure CLI authentication, not an interactive login. Azure
OpenAI requires an API version; Foundry v1 and other providers may leave it empty.

For local Ollama chat, enter `http://127.0.0.1:11434` and your installed model
(for example `shovel-chat-fast`); API version and API key can both be empty.
The runtime adds `/v1` for its OpenAI-compatible chat client.

Microsoft Foundry uses `model_provider_type = "azure_foundry"`. New configurations
use its OpenAI-compatible v1 Chat Completions endpoint:

```toml
[model]
model_provider_type = "azure_foundry"
base_endpoint = "https://YOUR-RESOURCE.services.ai.azure.com"
model_name = "YOUR-DEPLOYMENT"
api_version = ""
api_key = ""
```

A resource root gains `/openai/v1`; a complete `/openai/v1` base URL is preserved
(including a project-scoped v1 URL). `model_name` is the deployed model name.
An explicit key uses Bearer authentication; an empty key uses lazy, renewable
Azure CLI authentication with `https://ai.azure.com/.default`, never interactive
login or a fallback key. The read-only health check uses the same authentication.
Main Agent, Memory LLM and platform Skill/Agent clients share this factory.

Existing `azure_foundry` configurations with a resource root and non-empty
`api_version` retain the legacy versioned Azure OpenAI-compatible route.
For v1, leave `api_version` empty. Native `/models` inference and project-only
URLs are not OpenAI v1 base URLs and are rejected. This does not add Foundry
embedding or Anthropic Messages support. Your deployment must
support Chat Completions and the tools/vision/structured outputs you enable;
offline adapter tests do not certify every deployed model.
See [Microsoft Foundry endpoints and authentication](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints).

Select `api_mode = "responses"` in `[model]` and, if independently configured,
`[memory.llm]` for deployments requiring Responses instead of Chat Completions.
This uses MAF's Responses client for the Main Agent, platform Skill/Agent and
Memory, including streaming, function tools and structured output. Existing
profiles default to `api_mode = "chat_completions"`; no automatic fallback or
model-name guessing changes their protocol. The initialization wizard offers
both modes for OpenAI/Azure OpenAI/Foundry and recommends Responses for Foundry.
The workbench configuration editor exposes the same setting.

For a versioned Azure/Foundry deployment:

```toml
[model]
model_provider_type = "azure_foundry"
base_endpoint = "https://YOUR-RESOURCE.cognitiveservices.azure.com"
model_name = "YOUR-DEPLOYMENT"
api_version = "2025-04-01-preview"
api_mode = "responses"
api_key = ""
```

Versioned Responses requires `2025-03-01-preview` or newer. Foundry v1 also
supports Responses with an empty `api_version`; OpenAI uses its normal v1
base URL. Other providers retain Chat Completions. Responses sends `store=false`
and replays local Session history, not remote conversation/response IDs. This
does not certify arbitrary deployment features or change provider logging policy.
Changing only `api_version` does not select Responses.

The hidden chat API-key input is saved as `model.api_key`. The final
read-only model-list health check also uses Azure CLI authentication when this
key is empty; it does not send an inference request or launch interactive login.

Chat and Memory LLM use `api_key`, matching embedding configuration. Legacy
`security_key` fields and `SHOVEL_MODEL__SECURITY_KEY` /
`SHOVEL_MEMORY__LLM__SECURITY_KEY` environment overrides remain readable.
Newly generated or editor-saved files use `api_key`; loading alone does not
rewrite a file. Conflicting old/new names in the same source are rejected
without printing their values. Environment overrides still take precedence
over file settings regardless of which spelling is used.

Knowledge and Memory names accept only ASCII letters, digits and underscores
(not reserved filenames). Their stores are created under
`<root>\knowledge\<name>` and `<root>\memory\<name>`. The knowledge name
also sets `zvec.chunk_collection`. Embedding dimensions default to 1024 and set
both index dimensions. Ollama must produce the chosen
dimension natively; its unsupported `dimensions` request parameter is omitted.
Memory receives independent copies of the chat and embedding settings.

New profiles use chat budgets of 240000 input characters / 32768 output tokens,
300-second model/embedding timeouts, embedding batches of 8, 5 tool iterations
and 3 concurrent local-model requests. These initialization defaults do not
change existing profiles. Other settings remain available in the generated
file and the workbench configuration editor.

The default Windows configuration file is
`%USERPROFILE%\.shovel\config\settings.toml`. Re-running `shovel init` preserves
an existing file without prompting or overwriting it. For automated or
model-free setup, use `shovel init --no-input`; providers remain unconfigured
until edited. The workbench and keyword indexing can run without model credentials.

| Mode | Browser address | Backend |
| --- | --- | --- |
| Development | `http://127.0.0.1:3000` | FastAPI on `http://127.0.0.1:9999`; Next.js proxies `/api/*`. |
| Local production serving | `http://127.0.0.1:8080` | Python serves both the static frontend and FastAPI. |

Development supervises Next.js and the existing Generic Host together: either
exiting stops both. The ports are configurable through `dev_frontend_port`,
`dev_http_port`, and `http_port`.

Shutdown immediately closes knowledge-index admission. Shovel's Host adapter
uses one graceful deadline across service stopping and resource cleanup; dev
also checks frontend cleanup against that deadline and closes Node concurrently
with backend draining. Budget overruns are logged when they occur and produce
a nonzero exit status without replacing the original stop cause.
On Windows, dev creates the npm shim suspended, assigns a parent-owned
kill-on-close Job, then resumes it. Node descendants are contained even when
the shim exits first or the Python Host crashes.
The knowledge collection's entire open/query/write/flush/optimize/close lifecycle
runs in a private child process. Shutdown starts its admission drain immediately,
in parallel with other cleanup. It reserves up to five seconds (20% of the
remaining budget) to terminate and reap an unresponsive knowledge worker.
Writer ownership is released only after the actual child exits; the native I/O
locks and cancellation-safe drains remain in place. Windows uses a parent-owned
kill-on-close Job; Linux uses a parent-death signal. A child-owned collection lock
also prevents another Host or initialization from taking over a surviving writer.
Console interrupts belong to the Host, not Uvicorn or the knowledge worker.

Forced stops, channel failures and unexpected worker exits are **not successful
shutdowns**: they produce a nonzero exit status and retain the collection's
sibling `.worker-state.json` recovery journal. Reopening validates native metadata
and exposes `recovery_required`; it does not claim full physical integrity or
silently replay an optimization with an unknown completion receipt. A subsequent
acknowledged close may be `clean` while that recovery warning remains set.
Inspect/reconcile uncertain maintenance and verify authoritative SQLite data;
do not delete the journal to claim recovery.

`[zvec].worker_memory_mb` defaults to 4096 MiB (256-65536), with SDK query and
optimization thread pools configured to two threads each. Windows additionally
caps the worker's private committed memory at that budget plus 512 MiB of
interpreter overhead. The Linux limit is the SDK memory budget, not an OS RSS
guarantee. These settings do not cap Memory, models or the parent Host.
Memory stays in its separate existing lifecycle and is not gated by knowledge
opening or maintenance. The default 60-second overall budget is still **not a
universal hard process-exit limit**: Memory, network cleanup, process creation/
reaping and filesystem operations can have their own non-cooperative tails.
Never force-kill or experiment on an original store to demonstrate the bound.

The workbench exposes Chat and its session tree, Search Documents, Resources,
Background tasks, Skills, Workflows, Proactive Services, Approval Center,
Activity, and Settings. Settings contains runtime configuration, native Memory,
MCP management, Agent instructions, and Language. Choose English or Simplified
Chinese in **Settings → Language**; English is the default and the preference is
saved in the browser. See the [frontend guide](frontend/README.md) for UI details.

### Chat knowledge retrieval

The composer separates knowledge intent from the search algorithm:
**Automatic** lets the Agent call built-in knowledge search when needed, without
prefetching on every message; **Always search knowledge** retrieves before
answering; **Do not search knowledge** removes and rejects built-in knowledge
search for that request. Memory, profile, history, attachments and external
tools remain independent. Natural-language routing in Automatic is a model
decision; choose Always search for a hard retrieval requirement.

REST messages, budget previews and AG-UI forwarded properties accept
`knowledge_policy: "auto" | "required" | "disabled"` (default `"auto"`).
Existing explicit `resource_ids` or non-`auto` `search_mode` still require
retrieval; combining these with `disabled` is rejected. Required retrieval
needs a text query, including when attachments are present. No matches or
unusable citations produce explicit answer warnings, not invented evidence.

Optional Automatic tool searches use keyword search immediately if the
configured vector index is not ready, **before** generating a query embedding.
Tool results report requested/effective modes, degradation reason and warnings;
the warning is also saved with the answer and sent in the final stream event.
Known index warmup failures and embedding/query errors are not keyword fallbacks.
Direct Search Documents and explicit vector/hybrid requests keep strict
behavior. With no embedding configured, `auto` normally uses keywords.

Background warmup still owns index opening. Optional searches do not start or
cancel it; if `performance.warm_index` is off, they remain keyword-only until
an explicit search or another index user opens the index. This boundary avoids
cold-index waits, **not** slow queries against an already-open native index.
Strict retrieval may still wait for initialization/native I/O; no unsafe native
query timeout has been added.

### Same-Host Zvec performance diagnostics

`GET /api/v1/status` passively exposes `host_id`, `runtime_initialization`,
`environment`, and `index_open`. It does not open or query the knowledge index,
inspect collection metadata, invoke a provider, or retry a failed opening.
`index_open` is null until an actual opening starts; afterward it retains the
latest attempt in this Runtime, including failures. It is not persisted across
Host restarts. An opening's `operation_id` is a diagnostic ID, not a durable
background Operation.

The opening snapshot separates application-lock wait, dispatch/thread queue,
SDK import, SDK runtime initialization, collection opening, and total worker
and caller wall time. Import timing may be near zero if another component
already imported the SDK. It includes UTC-second timestamps, `warmup`/`on_demand`
reason, attempt number, process CPU deltas, and activity before/after opening.
An in-progress snapshot is partial: null phases have not completed, and
`elapsed_ms` continues increasing. Failures expose exception classes, not
messages or paths. Cancellation drains native work; `caller_cancelled` can
therefore be true while the actual opening finishes `ready`.

`runtime_initialization.elapsed_ms` ends when Runtime initialization/recovery
finishes; it is **not** process startup or HTTP-listener binding time.
Knowledge warmup no longer globally gates unrelated background Memory work.
Foreground-idle gating and shared local-generation admission remain; this is
dependency isolation, not isolation from CPU, disk, or model contention.

Keep the existing Web Host running and use a separate terminal:

```powershell
uv run shovel diagnose retrieval "Your knowledge query" --repetitions 3 --budget-seconds 180 --output .\zvec-first.json
uv run shovel diagnose retrieval "Your knowledge query" --compare-query "Another knowledge query" --repetitions 10 --budget-seconds 300 --output .\zvec-repeat.json
```

These are **explicit real searches** and can call the configured, possibly paid
embedding provider. They do not invoke the chat model. Tests use synthetic
collections and fixture embeddings; diagnostics never run automatically.
The CLI must discover a running Host with the same workspace and non-secret
retrieval configuration (Zvec, embedding and performance settings). It never
falls back to a one-shot Runtime. A missing/mismatched Host is an error.

Each query case first runs strict `vector` retrieval, recording scope,
embedding, index readiness, native search, SQLite revalidation, hydration when
applicable, and retrieval-log persistence. It then reuses that **same in-memory
vector and collection** for serial native repeats, including SQLite candidate
revalidation. Resource selection is frozen and rechecked between samples;
revocation aborts the run. Only one diagnostic batch can run per Host.
Metadata is inspected **after** the queries so it cannot warm the first sample.

The report includes the same historical `index_open` and
`runtime_initialization` snapshots. In contrast,
`samples[].phases_ms.index_ready_ms` measures that query's remaining index wait;
a near-zero value after warmup does not mean collection opening was fast.

The report contains a stable `host_id` for this Runtime, a per-run diagnostic
ID, scope/configuration fingerprints, UTC-second query timestamps, individual
samples and repeat median/maximum query times. Native timing separates worker
queue, collection lock, `collection.query()`, result conversion and total sink
time. Query-call time includes SDK processing, not just internal C++ execution.
For process-backed knowledge, `native_ms`/`query_ms` measure the child's SDK
query, while `ipc_ms` accounts for the remaining parent RPC/serialization
overhead. `worker_queue_ms` and `worker_lock_wait_ms` describe the child;
the existing queue/lock fields and `total_ms` retain the parent-side boundary.
Process CPU time is the child's CPU; `host_process_cpu_ms` is recorded separately.
Host-side query trace spans and UTC timestamps include the RPC boundary.
Process CPU time includes other threads. Activity snapshots identify observed
foreground/warmup/legacy-operation interference but can miss brief activity.

Actual SDK metadata includes document count, index completeness, vector
dimension/metric/HNSW settings and the three production filter indexes.
Segment counts are explicitly unavailable with the current SDK. SQLite counts
cover eligible CHUNK states and the current model's indexed chunks; they are
not expected to equal collection totals, which include other resources and
potentially stale vectors.

For scope comparisons, repeat the command with `--resource <resource-id>`
(repeatable), then with no selection for all readable resources. For candidate
comparisons, vary `--limit`: the native candidate count remains
`min(300, limit * 10)`, so the default result limit of 6 searches 60 candidates.
These options affect only that diagnostic request, not production defaults.
Never remove authorization filters for a real-library comparison. Any
unfiltered control belongs in an independent synthetic collection.

Use the **same `host_id`** when comparing reports. A first observed query is
not proof of cold OS/disk caches. If readiness approaches zero and repeats
become much faster, investigate first-access costs. If query-call time stays
around ten seconds while queue/lock waits are low, investigate actual index,
filtering and storage rather than index initialization. If only full retrieval
is slow, inspect embedding, revalidation and hydration phases separately.

The sample budget defaults to 180 seconds (maximum 600); repetitions default
to 3 after each full retrieval (maximum 10), with at most two comparison queries.
It stops **subsequent** samples, not in-flight native work or final metadata
inspection. Disconnect/shutdown also prevent later samples, while cancellation
drains native work before releasing the diagnostic gate. Partial batches have
`status: "partial"` and CLI exit code 1. The transport has a 600-second read
timeout; a lost connection yields no complete report and is never auto-retried.

Reports omit query text, vectors, source excerpts, resource names and paths;
query text still appears in shell history and normal SQLite RetrievalLogs.
Treat reports as private operational metadata. `--output` requires a new file
and never overwrites an existing report; omit it to print JSON. The HTTP route
is `POST /api/v1/diagnostics/retrieval`, uses workspace/configuration binding,
and returns `Cache-Control: no-store`.

Physical disk I/O, hard page faults, storage medium and architecture emulation
are explicitly marked uncollected/undetermined. Correlate report timestamps with
Windows Performance Monitor or an independently approved OS trace when needed;
process I/O is not physical disk I/O. Diagnostics do not clear OS caches,
compact/rebuild collections, change HNSW parameters or stop other tasks.

For an offline synthetic baseline, the diagnostics tests use a separate
one-vector, four-dimensional collection: three sequential fresh processes
open/close it, and two fixture-query cases each repeat five times in one
Runtime. Run from the repository root:

```powershell
uv run pytest backend\tests\test_retrieval_diagnostics.py::test_synthetic_native_open_baseline_across_three_processes backend\tests\test_retrieval_diagnostics.py::test_real_sdk_diagnostic_is_serial_sanitized_and_uses_actual_metadata --no-cov -q -rP
```

These are correctness/measurement fixtures, not a representative large-library
performance target. Native-opening timings exclude Python process startup,
and OS cache state is uncontrolled. For a real baseline, arrange an approved
maintenance window: retain the same data/configuration/SDK/machine, capture
status and same-Host reports over at least three orderly Host starts, and use
at least five query repeats. Report medians/ranges rather than a tiny-sample
P95. Never open an active Host's collection in another process, copy a live
writing collection, or invoke real embedding diagnostics without approval.

### Python-only local production serving

```powershell
Set-Location frontend
npm run build
Set-Location ..
uv run shovel run prd
```

Next.js exports `frontend\out`; production needs no Node.js server.
Static assets resolve in this order: `--frontend-dir`, `frontend_path`,
packaged `shovel\static`, then the source checkout's `frontend\out`.
Missing assets fail startup rather than falling back to a development server.

This is a local hosting mode, **not the protected enterprise installation
procedure**.

## Configuration and models

**Settings → Runtime configuration** retains live provider, capacity and warmup
monitors and adds a grouped editor for the current user's
selected root's `config\settings.toml`. The editor shows file values separately from the
active process and environment-overridden values. It cannot select another
configuration file or change `agent.workspace_path`, `database.url`,
`zvec.knowledge_path` or `memory.index_path`. Other undisplayed settings are
preserved. The unused legacy `[document]` section is ignored on load and
removed when saving; newly generated profiles omit it. Saving rewrites TOML
formatting and comments.

Saves use a content revision and atomic replacement; conflicting edits require
reloading and merging. Credentials have separate **Keep**, **Replace** and
**Clear** actions and saved keys are never returned to the browser. Empty Azure
keys retain non-interactive Azure CLI authentication. Environment variables
still take precedence, including whole-section JSON overrides.

All settings in this editor deliberately require a **full Host restart**;
there is no partial hot rebind. Stop Shovel and rerun `shovel run dev` (both
processes) or `shovel run prd`. Reopen the configured frontend address/port,
then select **Check application**. Saving, or restarting only the Agent, does
not apply these settings. The check compares the active process with the file
plus current environment; it does not restart the Host or test provider
connectivity. Changed environment overrides remain visibly identified even
when the active process matches. Listener edits remain loopback-only;
Agent streaming shares the existing FastAPI listener.

`model.max_output_tokens` is a user-configurable output budget per model request,
not a whole-task quota. It defaults to 2048 and accepts positive integers without
a Shovel-imposed maximum. Choose a budget within your deployed model's output
and context limits; reasoning tokens may consume this budget too. Larger budgets
can increase latency and cost, and unsupported values can be rejected by the
provider. An independently configured `memory.llm.max_output_tokens` follows the
same rules; when Memory has no configured model, it uses the Main Agent settings.

`model.max_input_chars` is Shovel's local input character budget, not the model's
token context window or an API parameter. It defaults to 24000, retains a minimum
of 2000, and has no fixed Shovel configuration maximum. The runtime still bounds
context assembly and rejects requests exceeding the configured budget; removing
the configuration ceiling does not disable these guards. Characters do not map
one-to-one to tokens, so leave room for output and check the deployed model's
actual context limits. Independently configured `memory.llm.max_input_chars`
uses the same configuration rules and retains its extraction/summary guards.
Embedding input budgets and attachment size/count limits are separate.

The chat editor offers the tested Azure OpenAI, DeepSeek, QianWen and Ollama
scope independently for Main Agent and Memory LLM. Embedding offers Azure
OpenAI, QianWen with an explicit embedding endpoint, and Ollama; DeepSeek chat
support is **not** embedding support. Adapter availability is not live
provider/model certification. Existing other providers are preserved rather
than silently replaced. Vector identity/dimension/Memory-collection changes
are rejected while the corresponding index directory contains files: arrange
a controlled rebuild outside this editor first, without moving the fixed paths.
Changing configuration never deletes, migrates or automatically rebuilds data.

Opt-in live acceptance for this editor reuses the selected model configuration
but isolates SQLite, both vector indexes, temporary files and the workspace.
It calls the real Main Agent, Memory LLM and both embedders, round-trips every
editable field, and starts/stops separate real Host processes to verify changed
ports, preserved Sessions, environment overrides and provider-failure recovery:

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = "C:\path\to\settings.toml"
$env:SHOVEL_RUNTIME_CONFIG_LIVE_REPORT_DIR = "C:\path\to\acceptance-reports"
uv run pytest backend\tests\test_runtime_configuration_live.py --no-cov
```

These tests can incur provider charges and require existing non-interactive
authentication. They do not change the selected configuration or user stores.
Build `frontend\out` before running them. Reports omit keys, endpoints, source
paths and model response bodies. Passing against one configuration does not
certify other providers or API-key authentication when the selected Azure
configuration uses CLI authentication.

Edit the generated settings file for your machine. The
[complete Ollama example](Documents/settings.example.toml) includes chat,
knowledge embeddings, independent Memory embeddings, SQLite, Zvec, and
performance settings. Its paths and budgets are examples: adapt them instead
of copying another machine's configuration unchanged.

```powershell
uv run shovel config --check
```

Configuration precedence is environment variables over TOML over defaults.
Environment names use the `SHOVEL_` prefix and `__` for nested fields, for example
`SHOVEL_MODEL__MODEL_NAME`. Restart the Host after changing model or storage
configuration. Browser-entered source paths refer to the **Host machine**, not
the browser's filesystem.

### Independent provider responsibilities

| Configuration | Responsibility |
| --- | --- |
| `[model]` | Main Agent chat and tool reasoning. |
| `[embedding]` and `[zvec]` | Knowledge embeddings and their index. |
| `[memory.llm]` | Memory extraction/summarization; falls back to `[model]` when not separately configured. |
| `[memory.embedder]` and `[memory]` | Independent Memory embeddings and their index/dimension. |

Memory extraction uses JSON Object output for `deepseek` and `qianwen`, with
the expected schema in the instructions and local field/evidence validation.
Their Memory extraction and summary requests disable thinking and send the
configured `max_tokens` budget. Azure OpenAI, OpenAI and Ollama retain JSON Schema
output; select a model/deployment that supports the corresponding output mode.
No additional configuration switch is required when changing these providers.

Knowledge embedding configuration does not automatically configure Memory
semantic recall. Without a Memory embedder, SQLite facts remain available, but
semantic Memory recall is explicitly unavailable.

Embedding dimensions must match their existing collections. Changing a model,
dimension, or embedding identity requires the corresponding supported rebuild
or a new index; do not reinterpret old vectors. `embedding.revision` can
invalidate embeddings when weights change behind an unchanged deployment name.

`model.max_input_chars` budgets **characters, not tokens**, including actual
instructions, references, history, and tool results. The final context middleware
rejects overflow. Choose budgets compatible with the model's actual token
context; a large character limit cannot enlarge it.

### Local Ollama example

Install and run Ollama separately. These commands explicitly download/create
models; Shovel does not install models or drivers:

```powershell
ollama pull qwen2.5:3b
ollama pull bge-m3
ollama create shovel-chat-fast -f .\Documents\shovel-chat-fast.Modelfile
```

Configure chat as `ollama` with `shovel-chat-fast`, and both embedding providers
as `ollama` with `bge-m3` when using the supplied example. Its 1024-dimensional
knowledge and Memory collections must remain separate.

Local model quality and latency depend on hardware, context, and model choice.
Offline fixtures are not proof that a particular model will extract facts or
cite sources reliably. Remote chat/embedding providers receive the text sent to
them and may charge for calls.

### Azure OpenAI with Azure CLI authentication

See the [complete Azure OpenAI configuration reference](Documents/azure_openai_configuration.md)
for chat, Memory, embedding model selection, authentication, and index migration.

For `azure_openai`, an empty `api_key` selects Azure CLI authentication.
Shovel uses the asynchronous `AzureCliCredential` and a cached bearer-token
provider that refreshes tokens before expiry. Credentials are acquired lazily
on model requests; startup does not sign in or run inference.

```toml
[model]
model_provider_type = "azure_openai"
base_endpoint = "https://YOUR-RESOURCE.openai.azure.com"
model_name = "YOUR-DEPLOYMENT-NAME"
api_version = "2025-04-01-preview"
api_key = ""
max_input_chars = 160000
max_output_tokens = 1024
timeout_seconds = 300
```

Use the resource root endpoint, your actual **deployment name**, and an API
version supported by that deployment. This client uses the versioned Azure
Chat Completions API, not the `/openai/v1` endpoint.

Install dependencies from the committed lock and sign in before starting the
Host:

```powershell
uv sync --python 3.14-x86_64 --locked --all-extras --dev
az login --tenant YOUR-TENANT-ID
uv run shovel run dev
```

The Host must run as the same OS user with access to that Azure CLI login and
`az` on its PATH. Grant that identity **Cognitive Services OpenAI User** (or
equivalent inference permissions) on the Azure OpenAI resource. Authentication
does not bypass resource networking rules or enterprise egress policy.
CLI login failures are reported explicitly; Shovel does not launch interactive
login or silently fall back to an API key.

A non-empty `api_key` preserves API-key authentication. Environment
overrides such as `SHOVEL_MODEL__API_KEY` take precedence over the empty
TOML value, so unset them when choosing CLI authentication. Do not supply a
manually acquired access token as `api_key` or `AZURE_OPENAI_AD_TOKEN`;
let the provider acquire and refresh tokens. CLI mode rejects a configured
`AZURE_OPENAI_AD_TOKEN` rather than letting a static token bypass renewal.
The generic `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_MODEL`, and
`AZURE_OPENAI_API_VERSION` variables do not configure Shovel; use the TOML fields
or their `SHOVEL_MODEL__...` equivalents.

Separately configured `[memory.llm]` can use the same Azure OpenAI settings with
an empty key, or remain local. Azure knowledge and Memory embedding now also
use CLI authentication when their `api_key` is empty; non-empty keys retain
API-key authentication. Existing `bge-m3` models, index dimensions, and paths
need not change just to upgrade the client implementation. Remote model calls
transmit the selected context to Azure and may incur charges. Restart the
entire Host after changing configuration.

### Framework embedding providers

Knowledge and Memory share Microsoft Agent Framework's `OpenAIEmbeddingClient`
and official `OllamaEmbeddingClient`. Shovel retains batching, bounded transient
retries, vector validation, and model-version fingerprints; SDK retries are
disabled so attempts are not multiplied.

| `embedding.provider` | Endpoint and model requirements |
| --- | --- |
| `azure_openai` | Resource root `base_url`, deployment `model`, and `api_version`. Empty `api_key` uses Azure CLI and automatic token renewal. |
| `openai` | Defaults to `https://api.openai.com/v1`; configure an embedding model and API key. |
| `qwen` / `qianwen` | Defaults to DashScope's China `compatible-mode/v1` endpoint; use an embedding model, not a Qwen chat model. Override `base_url` for other regions. |
| `ollama` | Defaults to loopback port 11434 and keeps native batched `/api/embed`; install an embedding model such as `bge-m3`. |
| `deepseek` / other compatible providers | Requires explicit `base_url` for a host that actually exposes `/embeddings`, plus a genuine embedding model. A chat-only API or model is not supported merely by selecting its provider name. |

The official Ollama connector currently uses a prerelease pinned in `uv.lock`;
do not update it independently of the supported MAF core. This implementation
does not invent an embedding model for a provider whose service only offers
chat, nor fall back to a different provider when an embedding request fails.
`SHOVEL_EMBEDDING__API_KEY` and `SHOVEL_MEMORY__EMBEDDER__API_KEY` override TOML
independently; leave them unset for Azure CLI mode. Changing actual embedding
models still requires the corresponding index migration.

For models/APIs supporting shortened embeddings, set `dimensions = 1024` in
`[embedding]` and/or `[memory.embedder]`, with the matching `zvec.dim` or
`memory.dim`. Shovel passes this optional parameter through MAF; omitting it
preserves model-default output and existing fingerprints. Configured dimensions
must match the index. The current Ollama connector rejects this option.
Changing dimensions or models requires regenerating the corresponding vectors,
even if the old and new models both output 1024 dimensions.

### Concurrent background scans

`[performance].max_concurrent_scans` controls simultaneous Scan Jobs in one
Host (default **5**, range **1-64**). Set it to `1` for serial scanning, or use
`SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS` as an environment override, then
restart the Host. Different resources can scan concurrently; all profiles and
jobs for the same resource remain serial. With the default configuration, up to
**five different resources** can scan at once; excess jobs are queued:

```toml
[performance]
max_concurrent_scans = 5
```

In the scan worker's queue, a non-scan operation is a barrier: active scans
finish before that operation runs. This includes queued resource changes,
cleanup, and root platform executions. Memory extraction and Execution
maintenance run in separate workers.

Discovery enumerates source items **outside SQLite write transactions**, then
persists batches of at most 100 items using short `BEGIN IMMEDIATE` transactions;
Web Search retains per-item commits. Waiting for directory, Outlook, or website
enumeration does not hold the database write lock, allowing other scans,
Job heartbeats, and Execution maintenance to proceed. SQLite still permits
only one writer at a time; concurrent scans do not make database writes parallel.

This limits jobs, not documents or embedding batch size. Zvec native writes
remain serialized, and Azure quotas can still limit embedding throughput.
Queued receipts and Job IDs remain durable; stopping the Host cancels and
drains in-flight scan tasks, requeuing interrupted scans for restart rather
than treating shutdown as user cancellation. Complete discovery and a successful
final-batch commit are required before deletion reconciliation; failed or
interrupted discovery cannot authorize deletion of unseen documents.
Already failed scans are not automatically restarted: inspect the error and
submit a new scan after correcting its cause.

## Knowledge and RAG

### First keyword-only loop

Create a folder containing readable documents, then register and scan it:

```powershell
uv run shovel resource add notes --kind local_fs --set 'root_path=C:\Notes'
uv run shovel scan run notes --no-embed
uv run shovel operation list
```

Resource creation/verification does not itself complete an ingestion scan.
Use Background tasks or Operation status to inspect completion, then search
through Search Documents. `--no-embed` keeps SQLite text keyword-searchable and
leaves chunks pending for later embedding; it does not claim vector indexing
has completed.

### Supported sources

| Connector | Supported boundary |
| --- | --- |
| Local folders | Metadata discovery and parsing of supported text, Markdown, text-readable PDF, and modern Office documents. Scanned-image OCR is not implemented. |
| Classic Outlook | Windows desktop Outlook and an existing profile under the same OS user; optional `outlook` dependencies. Not New Outlook, unattended mailbox access, or a standalone PST parser. |
| Public websites | Bounded public HTTP(S) GET crawling with origin/path/public-IP checks and robots/noindex/nofollow handling. Not authenticated browsing, JavaScript rendering, private-network access, or unrestricted proxying. |

Outlook ingestion does not read attachment bodies or send/change mail. Missing
mail does not imply deletion of its indexed copy. Website absence likewise
does not prove a complete discovery or authorize automatic erasure.

These personal/legacy connectors are **not automatically governed enterprise
adapters**. Do not treat importing a local file or mailbox as evidence of
enterprise policy enforcement or encryption. The controlled Kusto metadata
path is separate from ordinary scanning.

### Retrieval and recovery

Scanning follows discovery/fetch, parsing, parent/child chunking, embedding,
persistence, and reconciliation. Small child chunks are embedded; parent text
remains in SQLite for expanded answer context. Keyword retrieval uses SQLite
FTS5; hybrid retrieval combines keyword/vector ranks with reciprocal rank
fusion, then revalidates visibility and revisions against SQLite.

Paused resources remain searchable; revoked/deleted resources are excluded.
Full-sweep deletion requires complete discovery and the connector's
`DELETE_DETECT` capability. Partial discovery must not remove unseen documents.
Local fingerprints use size and nanosecond mtime, not content hashes; use
`--force` when intentionally preserving both while changing content.

Vector deletion intents commit with metadata changes through a durable
Deletion Outbox. Cleanup acknowledges them only after sink deletion/flush.
Vector persistence must succeed before chunks become indexed. After interruption,
use scan/cleanup status and supported reconciliation rather than editing SQLite
or deleting an active index directory.

For an explicitly acknowledged knowledge-index maintenance window, run
`uv run shovel index optimize --yes --concurrency 2`. This uses a durable
`index.optimize` Operation, drains accepted knowledge I/O and temporarily rejects
new vector requests; Memory and keyword search have no shared maintenance gate.
It is never automatic and does not change embedding or ANN query parameters.
Inspect `/api/v1/index/status` and the Operation receipt; a dispatched operation
with unknown completion must be reconciled, not blindly retried.

See [scan pipeline](Documents/scan_pipeline.md) and
[Outlook integration](Documents/outlook_local.md).

## Native Memory

Native Memory keeps four distinct layers:

| Layer | Purpose and scope |
| --- | --- |
| Conversation history | Persisted turns in one session. |
| Session Summary | Bounded summary for that session's continued context. |
| Long-term facts | Explicitly scoped facts with provenance, confidence, revision, expiry, and supersession; agent-scoped facts can cross sessions. |
| Working context | User-maintained Session/task goals, constraints and plans; explicitly selected, revision-checked, expiring, and not an execution authority. |

Memory is enabled by default per request. Automatic extraction reads only the
**current user's message**, using an isolated, tool-free MAF Agent, and runs
through durable Operations after foreground idle. Facts, extraction receipts,
and Memory Index Outbox records commit atomically in SQLite; Zvec follows
successful flush.

### Cross-session expectations

Answering a question in the original session can use its conversation history
without any long-term fact having been saved. A new session requires committed,
active facts in the matching workspace/user/agent scope.

Core profile questions use a bounded, deterministic SQLite fact lookup rather
than relying solely on vector similarity. Other recall can use the independent
Memory index and an independent SQLite keyword index. Without embedding, profile
and keyword recall remain available, but general semantic recall does not.
Query-matched facts take priority over background profile context under the
same item/character limits. A shared compact model projection preserves semantic
relationships and event times; complete provenance remains in SQLite and the
management API, traceable by fact ID and revision.
If a new session appears to forget a fact, check extraction
Operation status, saved facts/scope, expiry, and embedder availability before
assuming the chat model configuration is the only cause.

Only AG-UI **`RUN_FINISHED`** confirms the durable answer and queued Memory work.
It does not mean extraction or indexing has finished. Incremental streamed text
is not persistence confirmation.

### Lifecycle and deletion

Memory management in Settings/API supports sources, editing, history, review and forgetting.
Set `[memory].review_required = true` to keep newly extracted facts pending
human approval; the default preserves automatic activation. Rejected facts retain tombstones.
Recall revalidates vector hits against SQLite scope, state, revision, model
version, and expiry. Forgetting advances workspace/user generations so older
tasks and stale vectors cannot resurrect forgotten facts.

Deleting a session removes its history, summary, working tasks and session-scoped Memory,
not independently saved agent-scoped cross-session facts. Soft deletion retains
a tombstone. **Logical deletion is not physical erasure**, including of SQLite
WAL, vector files, backups, or underlying media.

Automatic Procedural Memory learning and Graph Memory are not implemented.
Versioned Skill Memory access is disabled in V1.

Start with the [Memory design and progress overview](Documents/Memory_Design/README.md).
See the [native Memory implementation](Documents/Memory_Design/native_memory_architecture.md)
for configuration, extraction, recall, recovery, and current limitations.
The [Memory completion roadmap](Documents/Memory_Design/memory_completion_roadmap.md) documents
implemented phases 1–3 and the separate future experience-learning stages.
The [real-model Memory baseline](Documents/Memory_Design/memory_baseline.md) separates extraction,
gold-corpus recall and end-to-end recall using isolated synthetic data, and records
the measured threshold/context-budget losses rather than assuming saved facts are recalled.
Recall now checks bounded entity/relation/applicability support after fusion, admits
only supported facts plus background Profile, and explicitly marks missing Memory
evidence after final context budgeting. The baseline also includes a frozen-corpus
real Main Agent answer evaluation; its small synthetic results are not a production
quality guarantee, and the default similarity threshold remains unchanged.

## Capability, Workflow, and Proactive Services

The platform separates stable Capability definitions from versioned
Function/Skill/Workflow/Agent content. Published content is immutable; editing
forks a new Draft. Invocation freezes exact versions, content hashes,
dependency locks, and permission ceilings before persistence.

The existing Execution engine owns root and child lifecycles, checkpoints,
approval waits, input waits, timers, and bounded retry. Its durable root reuses
the physical Operation infrastructure, not parallel task tables for each
capability kind. Legacy Chat, Job, and Memory lifecycles have not all migrated
into this engine.

| Component | Current behavior |
| --- | --- |
| Functions | Trusted registered contracts/handlers, not user import paths, `eval`/`exec`, or automatically loaded workspace Python. |
| Versioned Skills/Agents | Isolated, tool-free MAF Agents with frozen instructions, separate from Main Agent workspace extensions and Memory. |
| Workflow V1 | Trigger-agnostic serial DAG with Input, Function, Skill, Condition, Approval, Ask User, Notification, Emit Event, and Output nodes. |
| Proactive Services | Revisioned event/interval/daily configuration invoking a capability; edits save disabled revisions and require explicit enable. |

Workflow V1 does not implement Parallel, Join, Loop, Script, or external trigger
nodes. Emit Event is an execution step, not a trigger. Visual Agent authoring
and cron/watch scheduling are not implemented.

Permissions default empty. Exact `(action, resource)` grants intersect user,
platform, caller, capability, service, workflow/skill, parent, and frozen
boundaries. Child calls can only narrow authority. Authorization precedes Risk,
Policy, and Approval; **approval never supplies a missing permission**.

Publishing, enable/resume, approval/rejection, and input require an exact
server preview followed by matching intent and a single-use confirmation ticket.
Main Agent tools may return pending actions but cannot turn ordinary chat into
human confirmation.

Per-service pause preserves enabled state. Global Proactive pause blocks new
automatic admission, permits manual Run Now, and leaves existing executions and
timers active. Resume does not replay skipped occurrences. This business pause
is distinct from an enterprise security rollout pause.

The in-process EventBus is bounded and non-durable: only a committed Execution
crosses that persistence boundary; deduplication is not Exactly Once. Retry is
limited to explicitly transient, replay-safe work. An uncertain non-idempotent
external effect requires reconciliation, not blind retry.

Read-only CLI entry points:

```powershell
uv run shovel capability list
uv run shovel workflow list
uv run shovel proactive list
uv run shovel execution list
uv run shovel interaction list
```

See the [platform architecture](Documents/platform_architecture.md) and
[ADRs](Documents/platform_adrs.md) for definitions, authoring, and execution
contracts.

## Main Agent and workspace extensions

The Main Agent uses MAF's tool loop, sessions, context-provider hooks, and native
sliding-window compaction. Its context combines bounded history, Session
Summary, native Memory recall, and independent RAG evidence.

Extensions load lazily from `agent.workspace_path`:

| Extension | Convention |
| --- | --- |
| MAF Skills | `skills\<name>\SKILL.md`; advertise metadata, load content on demand. |
| MCP | Trusted stdio/HTTP servers configured in workspace `mcp.json`. |
| Python functions | `functions\*.py` exports `tools`. |
| Child Agent/Workflow tools | `pipeline\*.py` exports `create_agent(client)`. |

Python functions, pipelines, and Skill scripts require
`enable_python_extensions=true`, **disabled by default**. These execute trusted
local code; they are not a sandbox. Child Agents do not automatically inherit
the Main Agent's Memory, RAG, instructions, tools, or Skills.

Workspace `agent-instructions.md` appends custom instructions to retained
built-in rules. Instructions and MCP use revision-checked atomic UTF-8 saves.
Saving instructions restarts the Agent automatically; saving MCP does not.
See [MCP authentication](Documents/mcp_authentication.md) for API Key, static
Bearer/PAT and stdio credential references, compatibility, and the OAuth roadmap.
Restart after changing cached extensions/MCP, and finish active or queued chats
first. A saved-but-failed instructions restart remains visible and blocks chat
until recovery.

Review extensions and server destinations before enabling them. Prefer
environment/credential references instead of embedding secrets in workspace
files. None of these personal extension mechanisms supplies enterprise trust,
provider registration, or missing grants.

## Enterprise security and controlled deployment

### Purpose

The enterprise framework controls **who can access which source, for which use,
through which destination, under what current authority**, including child
execution, restart, revocation, and uncertain external effects. It does not
replace authentication with model instructions or treat a successful read as
permission for every downstream use.

READ, DISPLAY, model submission, Memory, indexing, persistence, and export are
separate decisions. Source policy, organization grants, caller authority, and
frozen execution boundaries must all permit the specific use.

### Implemented controls and remaining production requirements

| Boundary | Implemented mechanisms | Still required for accepted production |
| --- | --- | --- |
| Identity and installation trust | Windows native WAM bridge, protected Host bootstrap/IPC checks, complete-installation inventory tooling, organization-signing path, ACL/HKLM trust and upgrade/revocation controls. | Approved tenant/application/account/resource, organization keys and actual signed installation, broker/UI/cancellation and tamper/upgrade acceptance. |
| H1: information flow | Default-deny destination/use authorization, child narrowing, source revalidation, and containment across legacy outlets. | Qualified positive secure adapters for each permitted outlet. Negative containment is not proof every outlet safely operates. |
| H2: external effects | Durable effect receipts, dispatch/reconciliation boundaries, replay-safe retry rules, and restart/recovery controls. | Real external-write adapters and their idempotency/reconciliation acceptance; production external-write adapter maps are not enabled. |
| H3: source trust | Signed organization/source policy, metadata lineage and freshness/revocation checks, controlled Kusto metadata path. | Approved real Kusto resources, live adapter and revocation/freshness evidence. Business-row queries and model egress are not open. |
| H4: storage governance | Signed retention/offline/revocation/delete matrix contracts, storage admission/retirement proofs, retention fences, cleanup/backup/recovery acceptance contracts and isolated tests. | A concrete production `StorageAuthority`, actual encryption/key lifecycle, complete backup inventory, physical-erasure acceptance, and restore-set-external anti-rollback authority. |
| Live acceptance and rollout | Independent signed-report verification, separate organization approval, protected approve/enable/pause/revoke controls, frozen admission and execution/source/release rechecks. | Independently performed real installed measurements, retained receipts, exact final-artifact evidence, and formal organization approval of the target environment/scope. |

Ordinary SQLite/Zvec files, BitLocker status alone, an available keyring, or a
local ledger do not satisfy the H4 authority contract. SQLite/WAL/SHM/temp,
vectors, artifacts, caches, backups, consumer-bound access, and key revocation
must be covered by the actual implementation. HKLM generation checks do not
protect against administrator or whole-machine rollback; an external/TPM-backed
recovery authority remains a separate requirement.

Existing local data is not automatically encrypted or promoted into an
enterprise-approved store by this framework.

### Opening a controlled scope

The first supported rollout proposal is **one exact connection** for
`kusto.metadata.read`, exact `enterprise.read`, and the approved READ/workflow
state use. Optional DISPLAY/user-screen release needs separate approval and an
appropriate adapter. This does not enable business-row access, arbitrary
enterprise Function registration, or model processing of organization data.

Deployment must proceed through the protected installed bootstrap and native
administrator controls, not a TOML enable switch:

1. Establish the organization policy, concrete H4 infrastructure, identities,
   exact resource scope, and signing/verification trust.
2. Build and verify the complete controlled installation, inventory, artifact
   provenance, ACLs, and trust anchors.
3. Independently measure real installed WAM/adapters, restart/concurrency,
   revocation, IPC replay/forgery, credential containment, H2/H4, and recovery.
4. Sign the live report with the independent enrolled verifier; obtain separate
   organization approval binding the exact plan, installation, and evidence.
5. Preview and approve the exact bytes/generation, then explicitly enable.
   Monitor, pause, and revoke through protected controls.

`SignedLiveAcceptanceAuthority` verifies signed attestations and bindings; it
does **not** perform or fabricate those live measurements. Offline tests, native
platform checks, CI success, and provenance cannot substitute for them.

Approval is not enablement. Each control transition advances generation; old
executions cannot rebind to a later approval, including after pause/re-enable
or restart. Runtime checks the frozen rollout at admission, dispatch,
authentication/source access, use, and final release. An already-sent request
cannot be retracted; post-I/O rechecks discard unauthorized late results.

The gate remains closed when trust, policy, adapters, storage authority,
qualified evidence, or current approval is missing. It never bypasses ordinary
permissions, Risk, Approval, or H4 to make a demo succeed.

See [native installation and rollout](native/README.md),
[enterprise implementation status](Documents/enterprise_security_analysis.md),
and the [threat model](Documents/enterprise_threat_model.md). The
[provider guide](Documents/enterprise_provider_guide.md) describes authoring and
synthetic examples; earlier stage notes are not current production approval.

## API and recovery contracts

Web, CLI, and Main Agent platform tools share `PlatformApplication` and the
existing Host. Legacy resource/scan/Memory mutations use the Operation queue.
CLI commands use the matching running Host or a short-lived Generic Host;
read-only legacy queries can access SQLite directly. OS locks prevent competing
Runtime writers.

| Contract | Behavior |
| --- | --- |
| REST | `/api/v1`; collections return `{"items": [...]}`. |
| Durable legacy mutations | HTTP 202 plus Operation, not a completed resource. |
| Platform run/resume | HTTP 202 plus Execution, not a completed result. |
| Agent streaming | AG-UI `POST /api/v1/agent` on the existing FastAPI listener; no separate Agent listener. |
| Human confirmation | Exact preview, matching intent/IDs, single-use process-local ticket expiring after five minutes; durable ApprovalRequests are separate facts. |
| Time/progress | Numeric UTC Unix seconds, not milliseconds; scan progress is a fraction from 0 to 1. |

Preserve `Idempotency-Key` across lost responses and retries; changing arguments
under the same key conflicts. CLI `--request-key` provides explicit request
identity. Inspect the original receipt before resending an uncertain mutation.
The browser persists workspace-scoped recovery receipts; Activity owns platform
run recovery separately from legacy Background tasks.

After a lost Agent stream, inspect persisted history before sending the same
message again. Preserve `no-cache, no-transform` streaming headers: proxy
buffering must not turn partial text into a false completion signal.

Explicit history maintenance is available through
`shovel history prune --keep-days 30 --limit 1000` with confirmation. It retains
active work, pending cleanup, documents, conversations, and keyed receipts.
Personal history pruning is not an automatic organization retention policy.

## Development, CI, and packaging

### Repository layout

| Path | Responsibility |
| --- | --- |
| `backend\src\shovel` | CLI, Generic Host/Runtime, Agent, API, Capability/Execution platform, scanning, Memory, enterprise/security/protection domains and adapters. |
| `backend\tests` | Offline domain, Runtime, API, recovery, platform and security regressions; explicitly opted-in live tests. |
| `frontend` | Static-exportable Next.js workbench; backend functionality belongs in FastAPI, not production Next.js routes/server actions. |
| `native` | Windows WAM bridge, protected installation/bootstrap, and administrator rollout controls. |
| `scripts` | Packaging, installed-wheel smoke, synthetic acceptance and evidence tooling. |
| `Documents` | Configuration, architecture, ADRs, implementation evidence, and boundaries. |

### Local quality checks

From the repository root:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy backend\src
uv run pytest
```

Pytest discovers `backend\tests`, adds `backend\src`, enables automatic asyncio
handling, and includes coverage by default. Focused examples:

```powershell
uv run pytest backend\tests\test_chunker.py::test_same_input_yields_identical_ids --no-cov
uv run pytest backend\tests\test_scan_concurrency.py backend\tests\test_scan_pipeline.py --no-cov
uv run pytest backend\tests\test_native_memory.py backend\tests\test_memory_tasks.py --no-cov
uv run pytest backend\tests\test_platform_demos.py --no-cov
uv run pytest backend\tests\test_enterprise_rollout.py --no-cov
```

Offline tests do not require model credentials. Runtime tests must isolate
SQLite, workspace, knowledge-vector, and Memory-vector paths so a running local
Host does not hold their locks.

The scan regressions cover concurrent capacity, slow discovery alongside
maintenance and heartbeats, cancellation, same-Job restart recovery, and
partial-discovery deletion safeguards. These are isolated offline fixtures,
not live mailbox, provider-throughput, or release acceptance measurements.

From `frontend\`, after restoring the committed package lock:

```powershell
npm run lint
npm run typecheck
npm test
# One test file, using the same Node runner as npm test:
node --experimental-strip-types --test tests\i18n.test.mjs
npm run build
```

Before frontend changes, read `frontend\AGENTS.md` and the relevant installed
Next.js guide. This project uses a canary release; preserve static export.
Native development uses the SDK pinned by `native\global.json`; see
[native validation](native/README.md) for its checks.

### Live tests are explicit

Knowledge live baselines require opted-in, configured chat and embedding
providers and isolated test stores:

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = 'C:\path\to\settings.toml'
uv run pytest backend\tests\test_knowledge_baseline.py -m live --no-cov
```

These can call paid external services and send synthetic evidence to them.
They are model-quality measurements, **not enterprise installed-live acceptance**.

Image attachment live tests use the same explicit opt-in and selected configuration:

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = 'C:\path\to\settings.toml'
$env:SHOVEL_ATTACHMENT_LIVE_REPORT_DIR = 'C:\path\to\reports'
uv run pytest backend\tests\test_session_attachments_live.py -m live --no-cov
```

These require a vision-capable chat model and send only synthetic high-detail
images and test prompts, using isolated stores and workspace. REST and AG-UI
each verify single-image recognition, multiple images, retained image history,
and media-limit rejection without additional provider requests. The configured
text budget is capped at the default 24,000 characters in the test runtime to
exercise the original base64-budget regression; the configuration file is not
modified. Reports contain synthetic answers, model identity, image counts and
request sizes, not credentials or request bodies. The report directory is
optional and defaults to pytest's temporary directory. Without opt-in, these
tests are skipped and do not call the model.

### Packaging and evidence

A source-only `uv build` does not build or bundle the workbench automatically.
For a wheel with frontend assets, use a clean packaging checkout:

```powershell
Set-Location frontend
npm run build
Set-Location ..
New-Item -ItemType Directory -Force backend\src\shovel\static | Out-Null
Copy-Item frontend\out\* backend\src\shovel\static -Recurse -Force
uv build
```

CI installs the wheel into a separate environment and uses **that environment's
Python**, not `uv run`, to execute `python -m scripts.smoke_enterprise_wheel
--wheel <wheel-path> --output <receipt-path>`. The wrapper verifies installed
package bytes against the wheel, invokes `scripts\smoke_wheel.py` for bundled
assets/runtime behavior, and checks synthetic enterprise contracts and default
denial. Its receipt is offline packaging evidence, not live deployment approval.

[CI](.github/workflows/ci.yml) covers Python lint/types/tests, frontend
lint/types/tests/export, wheel smoke, Windows native tests, Outlook/shared
pipeline/private-IPC regressions, and controlled-installation candidate
inventory. Windows offline lanes do not log into WAM or read a live mailbox.
Main-branch provenance jobs attest qualifying candidate artifacts after the
required lanes succeed; they do not issue organization trust or live approval.
See [Actions](https://github.com/hylinux/shovel/actions) for actual run status and
[release workflow](.github/workflows/release.yml) for release automation.
Release wheel validation installs dependencies from `uv.lock`, then installs the
built wheel with `--no-deps` to avoid resolving newer, untested dependencies.
To retry a release without moving its tag, run the Release workflow from `main`
with the existing version tag in its required `tag` input. It checks out that
tag and verifies it matches the project version before building and publishing.

Historical validation reports describe the commits/environments measured.
Neither old failures nor old successes establish the result for a new final
commit; release evidence must match its exact artifact and revision.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Development environment](prepare-develop-env.md) | Windows/x64 setup and local model preparation. |
| [Complete settings example](Documents/settings.example.toml) | Independent chat, knowledge and Memory configuration; adapt machine-specific paths. |
| [Ollama Modelfile](Documents/shovel-chat-fast.Modelfile) | Local `shovel-chat-fast` model creation. |
| [Frontend workbench](frontend/README.md) | Navigation, streaming, Settings, MCP and frontend development. |
| [Platform architecture](Documents/platform_architecture.md) | Capability, Execution, Workflow, Proactive and persistence boundaries. |
| [Platform ADRs](Documents/platform_adrs.md) | Design decisions and compatibility invariants. |
| [Platform validation](Documents/platform_validation.md) | Historical demos/invariant evidence, not a current release certification. |
| [Enterprise implementation status](Documents/enterprise_security_analysis.md) | Security implementation stages, evidence and remaining production gates. |
| [Enterprise threat model](Documents/enterprise_threat_model.md) | Trust boundaries, controls, residual risks and acceptance requirements. |
| [Enterprise provider guide](Documents/enterprise_provider_guide.md) | Provider authoring and isolated synthetic demonstrations. |
| [Native installation and rollout](native/README.md) | WAM, complete-installation trust, signed live reports and administrator controls. |
| [Scan pipeline](Documents/scan_pipeline.md) | Ingestion, durable cleanup and recovery. |
| [Outlook integration](Documents/outlook_local.md) | Supported desktop environment and indexing limits. |
| [Memory design and progress](Documents/Memory_Design/README.md) | Design principles, delivered features, evaluation evidence and remaining work. |
| [Native Memory](Documents/Memory_Design/native_memory_architecture.md) | Extraction, recall, lifecycle, transactions and recovery. |
| [Memory vector identity](Documents/Memory_Design/db_base_memory_vector_id.md) | Deterministic identity, authority checks and rebuild semantics. |
| [Architecture design](shovel-architecture-design.md) | Broader architecture and module boundaries. |
| [Product design](shovel-architecture-design-v2.md) | Long-term goals, not an implemented-feature checklist. |
| [Learning roadmap](shovel-learning-roadmap.md) | Project-oriented learning stages. |

## License

[MIT](LICENSE).
