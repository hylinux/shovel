# Phase 13 final integration and architecture validation

Validation date: 2026-10-30. **Integration evidence is complete, but the repository
is not fully green or release-ready:** three existing MCP dependency failures and
88 existing whole-repository formatting failures remain.

No product feature, public API, permission grant, real external-write adapter or
database migration was added in this phase. The five demos use existing
application services and isolated stores. Missing CLI/Agent editing/global-control
surfaces are recorded rather than implemented, as explicitly confirmed for
Demo 5.

## 1. Implemented architecture

One existing Generic Host/DI composition owns the new capability registry,
InvocationService and ExecutionEngine. Function/Skill/Workflow/Agent executors
share the durable `operation` root. Automation matches configuration and invokes;
it does not implement email/briefing business logic.

New Main Agent, FastAPI, Typer/Rich and Next.js entries share PlatformApplication.
Authorization, risk, policy, approval and version/dependency checks remain
server-side. Details and a Mermaid diagram are in
[overall architecture](platform_architecture.md); the fifteen decisions are
recorded in [ADRs](platform_adrs.md).

This is the accepted **kernel-first migration**, not a claim that legacy
Operation/Job, Chat, Memory and optional workspace-code paths have already been
fully converted.

## 2. Final invariant compliance matrix

`Compliant` below describes the new platform boundary. The last column prevents
incorrectly extending that claim to legacy paths.

| # | Invariant | New platform | Evidence | Whole-repository qualification |
| --- | --- | --- | --- | --- |
| 1 | Capability never owns Trigger | Compliant | `capability\domain.py`; Demo 1 shares one Workflow across manual/scheduled admission | No trigger fields added to capability content |
| 2 | Proactive Service has no business logic | Compliant | `automation\manager.py`; Demo 2 keeps email analysis/draft/send in Workflow children | Matching/mapping is orchestration, not business execution |
| 3 | One durable Execution model | Compliant | `db\model\runtime.py`, `execution\domain.py`, kernel persistence tests | Partial globally: legacy Job/Chat/Memory lifecycles and unused FactTask scaffolding remain |
| 4 | One ExecutionEngine | Compliant | `execution\engine.py`, `runtime\service.py`, child execution tests | One Runtime composition, but legacy dispatch is not yet unified into the engine |
| 5 | Published CapabilityVersion immutable | Compliant | Registry/repository/SQLite guards; Demo 4 | Separate availability controls are mutable; published content is not |
| 6 | Version and Dependency Lock frozen before execution | Compliant | `execution\invocation.py`; Demo 4 publishes V2 while A is actually running | Unavailability fails explicitly, never selects a replacement |
| 7 | Permission and Approval independent | Compliant | `security\services.py`; security tests and Demo 2 | Partial globally: legacy extension effects do not pass through this new gate stack |
| 8 | Permissions only narrow | Compliant | PermissionResolver; Demo 3 parent/child/service ceilings; Demo 2 live revocation | No retroactive guarantee for legacy arbitrary workspace tools |
| 9 | External effects pass every gate | Compliant | Gate order; trusted FunctionContract; Demo 2 revokes permission after preview at the send child | Production has no real external-write adapter; legacy paths are outside this guarantee |
| 10 | Entries share Application Services | Compliant | `platform\service.py`, Agent/API/CLI adapters; Demo 5 and no-ORM AST tests | Partial globally: legacy entry persistence is deferred |
| 11 | Event/durable Execution boundary explicit | Compliant | `automation\events.py`, `automation\manager.py`; admission/recovery tests | Process-local events may be lost before Execution commit; no Exactly Once claim |
| 12 | Business Schedule separate from Wakeup | Compliant | `automation\schedules.py`, `execution\control.py`; Snooze/recovery tests | Snooze persists an existing execution timer, not another business schedule |
| 13 | Workflow has no external Trigger | Compliant | Workflow schema rejects trigger/unsupported nodes; designer palette tests | Emit Event is an execution node, not an external trigger |
| 14 | No arbitrary user code | Compliant | Trusted function catalog, declarative mappings/conditions, tool-free Skill/Agent adapters | Partial globally: existing opt-in Python/MCP/workspace extensions remain |
| 15 | Main Agent drafts only; sensitive actions explicit | Compliant | Structured pending actions, explicit publish/approval tests, real UI previews | New platform guarantee only; legacy extension effects still require migration |

No parallel ProactiveTask/WorkflowTask/SkillTask lifecycle or second new runtime
was introduced. Physical subordinate Attempt, Step, Wakeup, Approval, Input,
Interaction and Delivery records are facts under the shared root.

## 3. File changes

Phase 13 changes:

| File | Change |
| --- | --- |
| `backend\tests\test_platform_demos.py` | Five deterministic integration demos using the actual registry, SQLite, application facade, engine, Workflow executor, security gates and MAF Skill adapter |
| `scripts\smoke_wheel.py` | Isolate workspace and Memory-vector paths alongside existing temporary SQLite/knowledge paths; normalize this touched file with the formatter |
| `Documents\platform_architecture.md` | Implemented planes, domain/application boundaries, security, persistence and limitations |
| `Documents\platform_adrs.md` | Fifteen ADRs with context, decision, alternatives, consequences, migration and compatibility |
| `Documents\platform_validation.md` | This final report and reproducible verification scope |
| `README.md` | Correct stale Workflow/scheduling status, link implemented architecture/ADRs/report and describe entry-surface limits |

Cumulative Phase 4-12 implementation remains in `capability`, `execution`,
`security`, `automation`, `workflow`, `interaction`, `platform`, repository/model
adapters, Host integration and the six new frontend platform modules. Earlier
worktree changes and user-supplied `Documents\Phase*.md` execution prompts were
preserved. Those prompts are not substituted for completed ADRs.

No commit was created. Generated package artifacts and browser drivers/data were
not added to the repository.

## 4. Database changes

Phase 13: **none**. Current schema remains **14**.

The accumulated platform uses existing `operation`, `schema_migration` and
`app_setting`. New tables across prior phases are:
`capability_definition`, `capability_version`,
`capability_version_availability`, `capability_dependency`, `execution_attempt`,
`execution_step`, `approval_request`, `proactive_service`, `execution_wakeups`,
`input_request`, `interaction`, `notification` and `notification_delivery`.

The existing operation root carries frozen capability/context/lock JSON,
lineage, claim/version/deadline/usage fields, Workflow checkpoint and timer/retry
control. Scoped idempotency, exact version uniqueness, stable child identity,
approval consumption and snapshot immutability are guarded in repositories and
SQLite. Migration tests include repeatability, old receipt preservation,
ownership validation, rollback on failure and request-ID preservation.

Global automation control uses a scoped AppSetting. It is not a new table,
execution lifecycle or business schedule. Existing SQLite/FTS5/Zvec authority and
rebuild boundaries remain unchanged.

## 5. API changes

Phase 13: **none**. Existing `/api/v1/platform` serves capabilities/versions,
options, proactive services/history, global automation state, executions,
receipts, approvals and interactions.

Typed POST `/commands` delegates to PlatformApplication. Run/resume admission
returns 202 + Execution; read collections return `{"items": [...]}`. New
invocations preserve request identity. Publish/enable/resume/approval/input use
exact server previews and one-time human tickets, never client authority flags.
Ordinary AG-UI chat and legacy resource APIs retain their existing contracts.

## 6. UI changes and five demos

Phase 13 adds no UI product code. The existing Next.js/AG-UI workbench and static
export were validated, not replaced with another frontend stack.

| Demo | Verified path and assertions |
| --- | --- |
| 1: Daily Briefing | Real Main Agent tool adapter → Invocation → Workflow, and daily Schedule → EventBus → Proactive → Invocation → the **same exact Workflow/hash/lock**. Both complete with the same result; repeated occurrence does not create another root. |
| 2: Important Email Assistant | `email.received` with normal importance produces no run; high importance invokes Analyze Email Skill → Generate Draft Skill → mandatory Approval → test-only Demo Send Function. External-write risk triggers all applicable root/node/child approvals. At the send child, permission is revoked **after preview**: confirmation is denied, no effect occurs; restoring authority plus a fresh explicit confirmation resumes to success. Startup recovery does not repeat the completed mock effect. Output explicitly says `demo_only=true, sent=false`. |
| 3: User Skill | Analyze Customer Incident V1 runs directly through the Agent tool, as a Workflow child and indirectly through a Proactive Service. Exact child version, correlation and narrowed frozen parent/service permissions are checked. |
| 4: Version Immutability | A is already running and its real MAF Skill request is held at the deterministic transport. Skill V2 and Workflow V2 are published. A completes using V1 and the unchanged original snapshot/lock; new exact-policy work uses V1 and latest-policy work resolves Workflow/Skill V2. |
| 5: User Control | Agent pauses a service; actual Typer commands use the real confirmation helper through ASGI HTTP to the shared API; UI-shaped commands change preference/quiet hours, save disabled revision and explicitly resume. CLI/Agent/API read identical state and explain the same last execution/global blocker. Real headless Edge additionally exercises actual UI fields, exact publish, restricted palette, pause/edit/enable, preference, quiet hours, global pause/resume and reload persistence. |

Demo 5 uses the confirmed scope: UI/API perform editing and global control;
CLI/Main Agent demonstrate their existing individual-control/explain/read
surfaces. It does **not** claim nonexistent CLI/Agent editing/global-control
commands are implemented. The backend cross-entry fixture shares one database;
the browser uses another isolated real Host/store with the same public APIs.

The Skill adapter is the real tool-free MAF adapter using HTTP MockTransport:
there are no live paid model calls. This verifies orchestration and frozen
instructions, not model reasoning quality. The mock mail function is installed
only by the test fixture's trusted catalog; production gained no mail sender.

## 7. Test results

All commands ran on Windows x64 Python 3.14.7. The existing environment was kept
with `uv run --no-sync`; the lock was checked rather than silently regenerated.
Frontend validation used installed Node 26.13.0; CI still targets Node 22.

| Gate | Result |
| --- | --- |
| Full backend suite before Phase 13 edits | **725 passed, 3 failed, 1 live opt-in skip**, 328.49 seconds |
| Full backend suite with five demos | **730 passed, same 3 failed, same 1 live opt-in skip**, 333.08 seconds; 87% reported coverage |
| Final focused five demos, including mapped email/draft dataflow and send-boundary revocation | **5 passed**, 16.45 seconds |
| Migration/repository/kernel/security/Workflow/Host/API/CLI tests | Included in the complete backend run, including real offline CLI publish/run and no-ORM boundary tests |
| Ruff lint | Passed |
| Mypy | Passed, 183 source files |
| Formatter, new demo and touched smoke script | Passed |
| Whole-repository formatter | **Failed: 88 existing files remain**; baseline before edits had 89, with the touched smoke script normalized |
| Dependency metadata / lock checks | `uv pip check` and `uv lock --check --offline` passed; this does not prove runtime MCP API compatibility |
| Frontend lint / typecheck | Passed |
| Frontend tests | **35 passed**, no skipped frontend tests |
| Frontend production static export | Passed |
| Real headless Edge user-control scenario | Passed after correcting the test driver to validate before publishing and enable quiet-hours fields |
| Source distribution / bundled wheel build | Passed |
| Isolated installed-wheel smoke | Passed: bundled Web, REST, online CLI, profile request replay, keyword retrieval, graceful shutdown and released RuntimeLock |
| Diff whitespace | Passed |

The installed-wheel environment used a locked requirements export plus the
locally built wheel. The smoke asserts import from site-packages, not the
editable checkout. A temporary missing-httpx probe was resolved by installing
those locked dependencies into that isolated environment. The browser's missing
driver was restored only into the session tools directory. Neither operation
modified repository dependency manifests or the working environment.

### Failure classification

| Category | Evidence and disposition |
| --- | --- |
| Existing dependency compatibility failure | Three tests fail both before and after Phase 13 because `agent-framework-core` imports `mcp.Client`, absent from installed/locked `mcp==1.30.0`. A passing dependency-metadata check does not resolve this semantic incompatibility. No monkeypatch, test deletion or exclusion hides it. |
| Existing formatting baseline | Whole-repository formatter reports 88 remaining files, including old Python/Markdown snippets and line-ending issues. These were not mass-reformatted as unrelated work. Existing README code-fence formatting is part of this baseline. |
| Phase 13 regression | None observed: the same three backend failures remain; the five added demos pass and frontend/build/wheel gates pass. |
| Temporary validation environment | Empty wheel venv initially lacked httpx; session browser driver was absent. Restored in isolated directories, then their actual validation passed. |
| Deferred live validation | The repository's one live knowledge-baseline test retains its existing explicit provider/paid-endpoint opt-in. It was not newly skipped, deleted or enabled without consent. |
| Test-driver correction | Initial browser driver omitted the UI's required Validate step and quiet-hours checkbox. Driver corrected; no production bypass or product change was made. |

The three failing cases are:

- `test_real_mcp_transports_execute_and_close_on_owner_task[stdio]`
- `test_real_mcp_transports_execute_and_close_on_owner_task[http]`
- `test_mcp_restart_closes_real_connection_and_loads_new_config_without_losing_sessions`

The first two are in `backend\tests\test_agent_extensions.py`; the third is in
`backend\tests\test_hosted_runtime.py`. They prevent declaring a fully passing
backend or release gate.

### Reproduction

From the repository root, using the existing locked/synced environment:

```powershell
uv run --no-sync pytest backend\tests\test_platform_demos.py --no-cov
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy backend\src
uv pip check
uv lock --check --offline
Set-Location frontend
npm run lint
npm run typecheck
npm test
npm run build
```

Follow `.github\workflows\ci.yml` for packaging: bundle `frontend\out` into package
static assets, build the wheel, install it with locked dependencies in a separate
environment, then run `scripts\smoke_wheel.py` with **that environment's Python**,
not `uv run`. Do not overwrite an existing static directory or use production
stores for verification.

## 8. Security assessment

A dedicated read-only security review of the new platform changes reported no
high-confidence vulnerabilities. This is bounded review evidence, not proof
that legacy paths, dependencies or arbitrary hostile local processes are safe.

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| — | — | Reviewed new platform paths | — | No vulnerability reported | — |

Positive evidence includes send-boundary revocation after confirmation preview,
mandatory Workflow approval, one-time approval consumption, forged client-field
rejection, live dependency selection checks, permission narrowing and version
freeze. No real email, external business write or paid inference was performed.

Limits remain explicit: loopback/single-owner is not multi-user authentication;
legacy optional workspace Python/MCP code is outside the new gate boundary;
structured credential rejection is not unrestricted prose secret detection;
historical data was not scanned. No secrets were added to definitions, fixtures
or logs by these demos.

## 9. Backward compatibility

Existing resource/scan/chat/Memory routes, physical operation receipts, legacy
SQL NULL snapshots, static frontend serving and configured credential references
are preserved. No new Phase 13 schema, API or dependency change is required.

Old published serializer shapes/hashes remain protected by existing tests.
Publication and availability are separate; an existing execution never starts
using V2 merely because latest now resolves to V2. Pausing services/global
automation changes future automatic admission, not existing frozen work.

Older binaries are not promised to understand schema 14 platform rows. Rollback
of the accumulated schema requires a matching pre-upgrade database backup and
application build, not destructive ad-hoc table drops. Phase 13 itself has no
database rollback step.

## 10. Known limitations

The complete suite is not green because of the recorded baseline failures.
MCP end-to-end availability cannot be claimed for the current locked dependency
combination. Wheel smoke passes but deliberately does not exercise MCP.
Packaging also retains the existing warning that `uv_build>=0.13.5` has no upper
bound; a future breaking build-backend release remains a separate packaging risk.

Event delivery is process-local before admission; there is no durable event
ledger, complete skipped-event history or Exactly Once guarantee. Global resume
does not replay the backlog. Confirmation tickets are process-local and require
fresh preview after restart.

Real external-write adapters, connection-backed capability execution, live
provider/semantic-quality validation, Skill Memory and cross-entry editing/global
command parity are not delivered by these demos. Legacy lifecycle/ORM/code
boundaries remain partial at whole-repository scope.

## 11. Deferred work

Repair the existing MCP dependency contract and adopt a reviewed formatting
baseline without deleting or suppressing tests. Then migrate legacy lifecycle/
ORM/extension paths behind the accepted application and gate boundaries.

CLI/Main Agent preference/quiet-hours/global-control surfaces need a separate
approved product change. Real external adapters require credential resolution,
durable effect receipts, idempotency/reconciliation and fault-injection tests.
Durable broker/replay policy, richer workflow semantics, Memory-enabled Skills
and multi-user authentication remain separate future decisions.

## 12. Recommended next milestone

**Stabilize the existing release baseline first**, not another automation feature:
resolve the MCP runtime dependency incompatibility and the scoped formatting
backlog, then rerun the complete unfiltered backend/frontend/package gates on
Windows and CI's Node 22/Linux combination.

After that, explicitly plan legacy-boundary convergence. Do not silently enable
real external writes or broaden permissions as part of stabilization.
