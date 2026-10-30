# Shovel repository instructions

## Build, test, and lint

Use Python 3.14+, uv, Git, and Node.js 22.18+ (CI uses Node 22). Install from the
committed `uv.lock` and `frontend\package-lock.json`. `py-generic-host` is pinned to
a Git commit; no sibling checkout is required. On Windows, use x64 Python for
the native Zvec dependency, including under x64 emulation on ARM64:
`uv sync --python 3.14-x86_64 --locked --all-extras --dev`.
Keep Windows and WSL environments separate.

Run Python commands from the repository root:

```powershell
uv sync --locked --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy backend\src
uv run pytest
# One file or one test; --no-cov disables the default whole-package coverage report.
uv run pytest backend\tests\test_chunker.py --no-cov
uv run pytest backend\tests\test_chunker.py::test_same_input_yields_identical_ids --no-cov
# Deterministic platform integration demos; model responses and mail sending are test fixtures.
uv run pytest backend\tests\test_platform_demos.py --no-cov
```

Pytest discovers `backend\tests`, adds `backend\src` to the import path, enables
automatic asyncio handling, and includes coverage by default. Offline tests do
not require model credentials. Runtime tests should isolate SQLite, workspace,
knowledge-vector and Memory-vector paths; a running local Host may otherwise
hold the default Memory lock. Live knowledge baselines require explicit opt-in
and configured chat/embedding providers; they can call paid external services:

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = "C:\path\to\settings.toml"
uv run pytest backend\tests\test_knowledge_baseline.py -m live --no-cov
```

Run frontend commands from `frontend\`:

```powershell
npm ci
npm run lint
npm run typecheck
npm test
# One test file, using the same Node runner as npm test:
node --experimental-strip-types --test tests\request-recovery.test.mjs
npm run build
```

Initialize local configuration/stores with `uv run shovel init`, then use
`uv run shovel run dev` from the root. This supervises Next.js on port 3000 and
FastAPI on port 9999; either exiting stops both. Production is separate:
`npm run build` in `frontend\` exports `frontend\out`, then
`uv run shovel run prd` serves it from Python on port 8080, without Node.js.
Production asset resolution is `--frontend-dir`, `frontend_path`, packaged
`shovel\static`, then the source checkout's `frontend\out`.

For a distributable wheel, build the frontend, copy the contents of
`frontend\out` into `backend\src\shovel\static`, then run `uv build`.
CI additionally installs the wheel into a separate environment and runs
`scripts\smoke_wheel.py` with that environment's Python, not `uv run`, to verify
bundled assets and real installed-package behavior. The full quality gates are
in `.github\workflows\ci.yml`.

## Architecture

- The installed `shovel` entry point is the Typer app in `backend\src\shovel\cli`.
  `runtime\hosting.py` builds the Generic Host/FastAPI application and background
  workers; `container.py` owns dependency-injector providers and async resource
  lifetimes. `runtime\service.py` composes both legacy services and the Capability
  platform in the same Runtime.
- Legacy resource/scan and Memory mutations use the `OperationService` queue.
  The legacy CLI uses REST when a matching Host is running, otherwise a short-lived
  non-Web Host via `runtime\client.py`; read-only legacy CLI queries access SQLite directly.
  OS locks protect the database, knowledge-vector and Memory-vector directories
  against competing Runtime writers. Restart recovery preserves queued parameters
  and Job IDs.
- New Capability, Workflow, Proactive, Approval and Execution entry points share
  `platform\service.py`'s `PlatformApplication`. Agent tools, `/api/v1/platform`
  and CLI commands use this facade; ORM/SQL stay in repository/query adapters.
  `runtime\platform_client.py` uses the matching Web Host or the same short-lived
  Generic Host for both reads and commands.
- `capability\` separates Definition identity from versioned Function/Skill/
  Workflow/Agent content. `InvocationService` resolves and freezes exact versions,
  hashes, dependency locks and permission ceilings before persistence.
  `execution\engine.py` owns the single new Execution lifecycle; executors share
  it, including child calls. The durable root reuses physical `operation`, not
  parallel SkillTask/WorkflowTask/ProactiveTask tables. Legacy Job/Chat/Memory
  lifecycles have not all migrated into this engine.
- `automation\` matches revisioned Proactive configuration and declarative
  conditions/input mappings, then invokes a capability. ScheduleTriggerService
  only publishes NormalizedEvents. The bounded in-process EventBus is not durable:
  only a committed Execution crosses that boundary; dedupe is not Exactly Once.
  ExecutionWakeupService separately handles existing executions' timers/retries;
  RecoveryService runs under RuntimeLock. These workers reuse the existing Host.
- Workflows are trigger-agnostic Capability content with serial-DAG Input,
  Function, Skill, Condition, Approval, Ask User, Notification, Emit Event and
  Output nodes. Checkpoints live on the Execution root; node calls use child
  Executions. Versioned Skill/Agent executors use isolated, tool-free MAF Agents
  with frozen instructions, not mutable workspace extensions or Main Agent
  Memory. Skill Memory Access is Disabled in V1.
- Scanning flows through connector discovery/fetch, parser, parent/child chunker,
  embedder, persistence, and reconciliation. `pipeline\scan_service.py`
  coordinates this flow. SQLite stores authoritative document/chunk text,
  lifecycle state, jobs, receipts, and cleanup intents. Zvec is the rebuildable
  knowledge-vector index. Keyword search uses SQLite FTS5; hybrid retrieval
  combines keyword/vector ranks with RRF, then revalidates visibility and
  revisions against SQLite.
- Chat runs through Microsoft Agent Framework (MAF): `runtime\agent.py` constructs
  the main Agent with tools, context-provider hooks, `AgentSession`, and native
  sliding-window compaction. The context provider combines bounded SQLite
  history, Session Summary, native Memory recall, and independent RAG evidence.
  Chat persists turns and referenced excerpts, expanding child chunks into parent
  context. Both AG-UI `POST /api/v1/agent` and the non-streaming message API use
  this path on the existing FastAPI port; `agent_port` is not a separate listener.
- Native Memory uses SQLite as authority and a separate, rebuildable Zvec index.
  `memory\domain.py` and `memory\service.py` own policies; Protocols in
  `memory\ports.py` separate SQLite, Zvec and MAF adapters. `runtime\memory.py`
  assembles these, and `runtime\memory_tasks.py` runs durable Operations through
  the Host's Memory worker after foreground idle. History, Session Summary and
  long-term facts remain distinct. Memory defaults on per request; automatic
  extraction reads only the current user's message, using a tool-free MAF Agent.
  `[memory.llm]` defaults to `[model]`; `[memory.embedder]` and `memory.dim` are
  independent of knowledge embeddings. Without a Memory embedder, SQLite storage
  remains available but semantic recall is explicitly reported unavailable.
- The frontend is a static-exportable React/Next.js App Router workbench.
  `app\api.ts` centralizes REST access, polling, actions, and API types;
  `app\request-recovery.ts` persists uncertain mutation receipts. Development
  rewrites `/api/*` to FastAPI; production serves the export through
  `runtime\hosting.py`'s `mount_frontend` adapter.

## Repository-specific conventions

- Before frontend changes, follow `frontend\AGENTS.md` (also imported by
  `frontend\CLAUDE.md`) and read the relevant installed Next.js guide under
  `frontend\node_modules\next\dist\docs`. This project uses a canary release.
  Preserve static export: backend functionality belongs in FastAPI, not
  server actions or production Next.js routes.
- REST routes use `/api/v1`; collections return `{"items": [...]}`. Durable
  resource/scan/cleanup and Memory create/update/rebuild mutations return HTTP 202
  plus an Operation, not the completed resource. Preserve `Idempotency-Key` across lost responses and
  retries; changed arguments with the same key conflict. Keep browser recovery
  workspace-scoped and consistent with CLI request identity.
- Platform `execution.run`, `proactive.run` and `execution.resume` return 202 plus
  Execution. Published content is immutable; availability is separate. Editing
  forks a new Draft.
  Preserve old version hashes and request fingerprints when changing serializers;
  adding default fields must not rewrite existing published payloads or locks.
- Platform permissions default empty. `[platform.security]` supplies user/platform
  policy; `platform.agent_permissions` further limits the Main Agent.
  Permissions are exact `(action, resource)` pairs, intersecting configured
  user/platform grants with caller, capability, service, workflow/skill, parent
  and frozen boundaries. Child authority can only narrow. Authorization precedes
  Risk, Policy and Approval and is rechecked at dispatch/after approval; approval
  never supplies a missing grant. Any applicable approval requirement wins,
  including explicit Workflow Approval nodes and platform minimums.
- Capability publishing, service enable/resume, global automation resume,
  approval/rejection and input require an exact server preview (`prepare: true`)
  followed by the same intent and `confirmation_id`. Tickets are process-local,
  single-use and expire after five
  minutes; durable ApprovalRequests are separate facts. Main Agent tools return
  structured pending actions, never issue/consume human tickets or interpret
  ordinary chat as consent. Approval/input actions bind request ID, execution ID,
  scope and user. Interaction is a projection; Notification/Delivery is transport.
- Proactive edits save a disabled revision; explicit enable is required. Admission
  freezes the service revision/configuration. Per-service pause preserves enabled
  state. Global pause is a scoped AppSetting: it blocks automatic Event/Schedule
  admission only, permits manual Run Now and leaves existing executions/timers
  active. Global resume rechecks targets and does not replay skipped occurrences.
- Workflow V1 has no external trigger, Parallel, Join, Loop or Script nodes;
  Emit Event is an execution step, not a trigger. Function implementations resolve
  only trusted FunctionContract/handler registrations, never user import paths,
  eval/exec or automatically imported workspace Python extensions. Demo external
  writes belong in test fixtures, not the production catalog.
- Snooze/Delay suspend an existing Execution with `wake_at`, not a business
  Schedule. Retry only explicitly transient, replay-safe work with bounded
  attempts/backoff; dispatched non-idempotent external effects require
  reconciliation, not blind resume/retry. Completed children must not repeat.
- Only AG-UI `RUN_FINISHED` confirms the durable answer and queued Memory work;
  incremental text is not persistence, and completion is not completed extraction
  or indexing. On a lost response/stream, inspect persisted history before
  resending. Preserve the stream's `no-cache, no-transform` headers so the dev
  proxy does not buffer small SSE frames.
- Timestamps are numeric UTC Unix **seconds**, not milliseconds; frontend
  `date()` converts them. Scan progress is a fraction from 0 to 1. Use the
  shared frontend API/error/URL-segment helpers rather than separate fetch
  implementations.
- Frontend GETs share only in-flight transport through `app\read-requests.ts`,
  retaining independent cancellation and response bodies. Mutations invalidate
  reads; completed responses are not cached. Periodic queries pause in hidden
  tabs through `app\query-polling.ts`; keep explicit actions and Agent streaming
  independent of polling. Settings keeps visited tab panels mounted to preserve
  MCP/instructions drafts.
- Platform editors use server-authorized `/platform/options`; the server rechecks
  selections at save/validate/publish. Service controls reconcile acknowledged
  server revisions with older poll results through `observedService`; do not
  immediately reuse stale query revisions. Activity owns `execution.run`/
  `proactive.run` recovery receipts, separate from legacy Background tasks;
  look up the original request key before resending an uncertain run.
- Workspace extensions load lazily from `agent.workspace_path`: MAF Skills under
  `skills\<name>\SKILL.md` advertise metadata and load content on demand.
  `functions\*.py` exports `tools`; `pipeline\*.py` exports `create_agent(client)`
  returning a MAF Agent or Workflow, exposed as a tool. Python functions,
  pipelines and Skill scripts require `enable_python_extensions=true`, default
  false. Child Agents do not automatically inherit the main Agent's Memory,
  RAG, instructions or Skills. Restart Agent after changing cached extensions.
- Workspace `agent-instructions.md` appends custom text to retained built-in
  rules; missing/blank content uses defaults. Instructions and `mcp.json` share
  revision-checked atomic UTF-8 persistence in `runtime\workspace_config.py`.
  Instructions PUT saves and restarts automatically; MCP PUT only saves.
  Restart rejects active/queued chats and chat-model warmup, preserves sessions/Memory,
  and leaves model/MCP clients lazy. Save/restart cancellation must not permit
  new chats with stale instructions; saved-but-failed restart must stay visible
  and block chatting until recovery.
- `model.max_input_chars` budgets characters, not tokens. Include actual
  instructions, serialized references, history and tool results; the final
  `ContextBudgetMiddleware` rejects overflow. Main instructions do not replace
  the isolated Memory extractor/summarizer prompts.
- Connector configuration is persisted; capabilities and implementations live
  in `services\connector_registry.py`. Connectors implement the Protocol and
  exchange plain data, without database sessions or ORM objects.
  `ConnectorSpec.config_model` supplies validation and exposed JSON schema.
  Parsers are lazily registered in `pipeline\parsers\registry.py`, with extension
  routing taking precedence over document class; synchronous parsing runs via
  `asyncio.to_thread`.
- Small CHUNK records are embedded; PARENT records remain in SQLite for context.
  Preserve the shared deterministic `chunk_vector_id(..., resource_id=...)`
  identity across SQLite and Zvec. Changes to chunking semantics require
  `CHUNKER_VERSION` invalidation. Keep `context_prefix` separate from original
  SQLite text; embedding uses the prefixed text.
- Persist vector deletion intents in `DeletionOutbox` in the same SQLite
  transaction as metadata changes. Drain through `IndexCleanupService`;
  acknowledge only after successful sink deletion/flush. Vector persistence
  must succeed before marking chunks indexed. `--no-embed` leaves searchable
  SQLite chunks pending for later embedding, rather than claiming completion.
- Memory facts, supersession, `MemoryIndexOutbox` entries and extraction
  Operation receipts commit atomically in SQLite; Zvec acknowledgment follows
  successful flush. Reuse `memory_vector_id(kind, id, revision)` for upsert/delete.
  Revalidate vector hits against SQLite Scope, state, revision, model version
  and expiry. Forgetting fences older tasks with workspace/user `MemoryEpoch`
  generations; do not allow late extraction or stale vectors to resurrect facts.
  Soft deletion preserves a tombstone; session deletion removes its summary and
  session-scoped Memory, not agent-scoped cross-session facts. Automatic
  procedural learning is disabled. See `Documents\native_memory_architecture.md`
  for lifecycle, reconstruction, tests and currently unused ORM scaffolding.
- Full-sweep deletion reconciliation requires complete discovery and the
  connector's `DELETE_DETECT` capability, and respects profile ownership.
  Paused resources remain searchable; revoked/deleted resources are excluded.
  Outlook intentionally does not infer indexed deletions from missing mail.
- Schema upgrades are ordered, additive, repeatable steps in
  `db\migrations.py`, tracked by `SchemaMigration` and run at Host startup.
  Update upgrades as well as ORM models when changing existing stores.
- Runtime configuration uses `load_settings()` with environment over TOML over
  defaults (`SHOVEL_` prefix, `__` nesting). `AppSettings.load()` intentionally
  reads only the file. Preserve `SecretStr` values with Python-mode dumps for
  DI; configuration serialization must not replace real secrets with masks.
  Resource credentials use `identity_ref` and the OS keyring/environment
  references, never raw credentials in queued payloads. Capability connection
  requirements likewise store references, not secrets in versions or Workflows.
- Use `runtime.diagnostics.trace` for dev-only diagnostics, scoped by
  `development_logging()` in `run dev`. Log IDs, counts, and timings, not bodies,
  document text, source paths, or credentials. Browser console output is
  forwarded verbatim during development.
- Preserve the loopback-only, single-owner runtime boundary. For current
  behavior consult the root/frontend READMEs and CLI help; architecture design
  and roadmap documents also contain future plans.
- Implemented platform boundaries and decisions are documented in
  `Documents\platform_architecture.md` and `Documents\platform_adrs.md`.
  `Documents\platform_validation.md` records integration evidence and limitations.
