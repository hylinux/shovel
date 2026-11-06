# Shovel

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
| Knowledge and Memory | Durable ingestion, keyword/vector retrieval, citations, cross-session facts, index cleanup, and restart recovery are implemented. Model-dependent behavior still requires suitable models. |
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

`shovel init` initializes configuration and local stores. The default Windows
configuration file is `%USERPROFILE%\.shovel\config\settings.toml`.
The workbench and keyword indexing can run without model credentials;
configure providers before using chat or semantic retrieval.

| Mode | Browser address | Backend |
| --- | --- | --- |
| Development | `http://127.0.0.1:3000` | FastAPI on `http://127.0.0.1:9999`; Next.js proxies `/api/*`. |
| Local production serving | `http://127.0.0.1:8080` | Python serves both the static frontend and FastAPI. |

Development supervises Next.js and the existing Generic Host together: either
exiting stops both. The ports are configurable through `dev_frontend_port`,
`dev_http_port`, and `http_port`.

The workbench exposes Chat and its session tree, Search Documents, Resources,
Background tasks, Skills, Workflows, Proactive Services, Approval Center,
Activity, and Settings. Settings contains native Memory, MCP management, and
Agent instructions. See the [frontend guide](frontend/README.md) for UI details.

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

For `azure_openai`, an empty `security_key` selects Azure CLI authentication.
Shovel uses the asynchronous `AzureCliCredential` and a cached bearer-token
provider that refreshes tokens before expiry. Credentials are acquired lazily
on model requests; startup does not sign in or run inference.

```toml
[model]
model_provider_type = "azure_openai"
base_endpoint = "https://YOUR-RESOURCE.openai.azure.com"
model_name = "YOUR-DEPLOYMENT-NAME"
api_version = "2025-04-01-preview"
security_key = ""
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

A non-empty `security_key` preserves API-key authentication. Environment
overrides such as `SHOVEL_MODEL__SECURITY_KEY` take precedence over the empty
TOML value, so unset them when choosing CLI authentication. Do not supply a
manually acquired access token as `security_key` or `AZURE_OPENAI_AD_TOKEN`;
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
jobs for the same resource remain serial. A queued non-scan operation is a
barrier: active scans finish before resource changes, cleanup or platform work
execute. Memory retains its separate worker.

This limits jobs, not documents or embedding batch size. Zvec native writes
remain serialized, and Azure quotas can still limit embedding throughput.
Queued receipts and Job IDs remain durable; stopping the Host cancels and
drains in-flight scan tasks, requeuing interrupted scans for restart rather
than treating shutdown as user cancellation.

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

See [scan pipeline](Documents/scan_pipeline.md) and
[Outlook integration](Documents/outlook_local.md).

## Native Memory

Native Memory keeps three distinct things:

| Layer | Purpose and scope |
| --- | --- |
| Conversation history | Persisted turns in one session. |
| Session Summary | Bounded summary for that session's continued context. |
| Long-term facts | Explicitly scoped facts with provenance, confidence, revision, expiry, and supersession; agent-scoped facts can cross sessions. |

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
Memory index. If a new session appears to forget a fact, check extraction
Operation status, saved facts/scope, expiry, and embedder availability before
assuming the chat model configuration is the only cause.

Only AG-UI **`RUN_FINISHED`** confirms the durable answer and queued Memory work.
It does not mean extraction or indexing has finished. Incremental streamed text
is not persistence confirmation.

### Lifecycle and deletion

Memory management in Settings/API supports inspection and lifecycle operations.
Recall revalidates vector hits against SQLite scope, state, revision, model
version, and expiry. Forgetting advances workspace/user generations so older
tasks and stale vectors cannot resurrect forgotten facts.

Deleting a session removes its history, summary, and session-scoped Memory,
not independently saved agent-scoped cross-session facts. Soft deletion retains
a tombstone. **Logical deletion is not physical erasure**, including of SQLite
WAL, vector files, backups, or underlying media.

Automatic Procedural Memory learning and Graph Memory are not implemented.
Versioned Skill Memory access is disabled in V1.

See the [native Memory implementation](Documents/native_memory_architecture.md)
for configuration, extraction, recall, recovery, and current limitations.

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
| Agent streaming | AG-UI `POST /api/v1/agent` on the existing FastAPI port; `agent_port` is not a separate listener. |
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
uv run pytest backend\tests\test_native_memory.py backend\tests\test_memory_tasks.py --no-cov
uv run pytest backend\tests\test_platform_demos.py --no-cov
uv run pytest backend\tests\test_enterprise_rollout.py --no-cov
```

Offline tests do not require model credentials. Runtime tests must isolate
SQLite, workspace, knowledge-vector, and Memory-vector paths so a running local
Host does not hold their locks.

From `frontend\`, after restoring the committed package lock:

```powershell
npm run lint
npm run typecheck
npm test
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

CI installs the wheel into a separate environment and runs
`scripts\smoke_wheel.py` using **that environment's Python**, not `uv run`, to
exercise installed assets and real package behavior.

[CI](.github/workflows/ci.yml) covers Python lint/types/tests, frontend
lint/types/tests/export, wheel smoke, Windows native tests, Outlook/shared
pipeline/private-IPC regressions, and controlled-installation candidate
inventory. Windows offline lanes do not log into WAM or read a live mailbox.
Main-branch provenance jobs attest qualifying candidate artifacts after the
required lanes succeed; they do not issue organization trust or live approval.
See [Actions](https://github.com/hylinux/shovel/actions) for actual run status and
[release workflow](.github/workflows/release.yml) for release automation.

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
| [Native Memory](Documents/native_memory_architecture.md) | Extraction, recall, lifecycle, transactions and recovery. |
| [Memory vector identity](Documents/db_base_memory_vector_id.md) | Deterministic identity, authority checks and rebuild semantics. |
| [Architecture design](shovel-architecture-design.md) | Broader architecture and module boundaries. |
| [Product design](shovel-architecture-design-v2.md) | Long-term goals, not an implemented-feature checklist. |
| [Learning roadmap](shovel-learning-roadmap.md) | Project-oriented learning stages. |

## License

[MIT](LICENSE).
