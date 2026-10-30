# Capability platform ADRs

Status: accepted decisions as implemented in the Phase 4-12 **new platform**,
with the explicitly deferred legacy migration boundaries below. Phase 13 records
and verifies these decisions; it does not introduce another product milestone.
See [architecture](platform_architecture.md) and
[verification evidence](platform_validation.md).

## ADR 01: Single Durable Execution Model

- **Context:** Shovel already persists Operations, scan Jobs and Chat/Memory work.
- **Decision:** Reuse physical `operation` as the sole new Execution root;
  Invocation is admission, not another task lifecycle. Attempts/Steps are facts.
- **Alternatives considered:** New `execution` table plus legacy queue; separate
  ProactiveTask/WorkflowTask/SkillTask tables.
- **Consequences:** One new lineage/idempotency/state boundary; legacy adapters
  still require staged convergence.
- **Migration impact:** Add nullable kernel fields and scoped guards without
  reinterpreting existing resource/scan rows.
- **Current repository compatibility:** Preserves Operation/Job and existing
  commands. Does not claim that legacy Chat/Memory now share the new lifecycle.

## ADR 02: Single Execution Engine with Executors

- **Context:** Four capability kinds need different execution mechanics.
- **Decision:** One ExecutionEngine owns lifecycle; Function, Skill, Workflow and
  Agent executors return results/suspensions and call the same child engine.
- **Alternatives considered:** Runtime per kind; embedding lifecycle in executors.
- **Consequences:** Central budgets, transitions, claims and cancellation.
- **Migration impact:** Existing Host dispatches platform rows through the engine;
  legacy Operation dispatch remains a bounded transitional exception.
- **Current repository compatibility:** Existing Generic Host/DI composition is
  reused. The one-shot CLI Host is an alternative locked owner, not a new runtime.

## ADR 03: Capabilities Are Trigger-Agnostic

- **Context:** The same briefing/incident workflow must run manually or by event.
- **Decision:** Capability content and Workflow nodes contain no external trigger;
  automation references an exact or admission-resolved published version.
- **Alternatives considered:** Store cron/event nodes on Workflow/Skill versions.
- **Consequences:** Trigger changes do not publish or mutate business capability.
- **Migration impact:** No trigger is backfilled into existing capability content.
- **Current repository compatibility:** Existing AG-UI/CLI remain separate
  entry adapters; daily/event/manual demos invoke the same Workflow definition.

## ADR 04: Proactive Service Contains No Business Logic

- **Context:** Conditions, timing and user preferences must not become a second
  application/business engine.
- **Decision:** Store trigger, declarative conditions/mapping, capability target,
  boundaries/preferences and revision; delegate admission to InvocationService.
- **Alternatives considered:** Email/briefing business handlers inside scheduler;
  unrestricted expression or script engine.
- **Consequences:** ConditionEvaluator is pure data evaluation, not a trigger
  engine. Business behavior belongs to published capabilities.
- **Migration impact:** Add revisioned configuration only, not a task table.
- **Current repository compatibility:** ScheduleTriggerService publishes a
  NormalizedEvent; existing Hosted Services coordinate application services.

## ADR 05: Permission and Approval Are Independent

- **Context:** A user confirming an action must not manufacture authority.
- **Decision:** Authorize before risk/policy/approval, and reauthorize at execution
  and after approval. ApprovalRequest never contains new grants.
- **Alternatives considered:** Treat an approval click as permission elevation.
- **Consequences:** Revocation can prevent an already-previewed/approved action.
- **Migration impact:** Persist approval binding/user/execution/expiry separately
  from the frozen grant ceiling.
- **Current repository compatibility:** Configured policy and explicit action
  authorizers adapt existing single-owner settings without a permission UI.

## ADR 06: Permission Can Only Narrow

- **Context:** Agent, service, workflow and child boundaries overlap.
- **Decision:** Intersect all applicable configured, caller, parent, capability
  and frozen boundaries; require declared permissions to be a subset.
- **Alternatives considered:** Union inherited grants; child-selected overrides.
- **Consequences:** Removed permission is effective immediately; later policy
  expansion cannot enlarge an existing execution's frozen ceiling.
- **Migration impact:** Snapshot admission ceilings and propagate child context.
- **Current repository compatibility:** Exact permission pairs and default deny;
  no endpoint modifies configured user/platform authority.

## ADR 07: Immutable Published Versions

- **Context:** Running executions and approvals depend on stable semantics.
- **Decision:** Freeze published content/hash/provenance/dependencies. Editing
  forks a new Draft; separate availability records can revoke execution.
- **Alternatives considered:** Update a published row; let execution follow latest.
- **Consequences:** Availability can stop execution, never silently replace it.
- **Migration impact:** Capability/version uniqueness, repository checks and
  database immutability guards; preserve older serializer/hash shapes.
- **Current repository compatibility:** Definition metadata and lifecycle
  availability remain separate from version content.

## ADR 08: Dependency Lock

- **Context:** A frozen parent is insufficient if child versions can float.
- **Decision:** Publish exact dependency identifiers, numbers and hashes; freeze
  the validated lock into Execution before admission/dispatch.
- **Alternatives considered:** Resolve latest for every node; use version numbers
  without hashes; depend on mutable workspace paths.
- **Consequences:** Disabled/missing/changed dependencies fail explicitly.
- **Migration impact:** Persist dependency records and immutable snapshot content.
- **Current repository compatibility:** Workflow/Skill allowed references use the
  registry; old executions do not change when V2 is published.

## ADR 09: Durable Execution Boundary

- **Context:** The current event transport is in-process, not a durable broker.
- **Decision:** Only successful Execution persistence crosses the durable boundary.
  Keep stable admission identities; do not promise Exactly Once.
- **Alternatives considered:** Claim queue publication is durable; add an outbox/
  broker without an approved delivery/replay contract.
- **Consequences:** Pre-admission events can be lost; admitted work is recoverable.
- **Migration impact:** Reuse scoped execution request identities and receipts.
- **Current repository compatibility:** Bounded EventBus with isolation/drain fits
  the existing single-process Host without Redis or another database.

## ADR 10: Schedule Trigger versus Execution Wakeup

- **Context:** Calendar recurrence and a suspended execution have different owners.
- **Decision:** Business schedule publishes events; execution timers resume an
  existing root. Snooze is suspended + wake_at, never another business schedule.
- **Alternatives considered:** Cron per retry/Snooze; a scheduler executing business
  handlers directly.
- **Consequences:** Global automation pause does not stop existing execution timers.
- **Migration impact:** Separate `execution_wakeups` facts and proactive cursors;
  retain legacy schedule scaffolding without reusing it for Snooze.
- **Current repository compatibility:** Existing Hosted Services own polling and
  recovery. Interval/daily timezone rules are implemented; Cron is deferred.

## ADR 11: No Arbitrary User Code

- **Context:** Existing optional workspace Python extensions execute trusted local
  code, but cannot become the new platform's security boundary.
- **Decision:** New capability content is declarative; functions resolve only
  trusted catalog IDs; no user-controlled import, eval, exec or Script node.
- **Alternatives considered:** Python/JavaScript/Shell snippets; "sandboxed" eval.
- **Consequences:** Skills/Agents are tool-free; extensibility is narrower but
  authorization and effect metadata stay enforceable.
- **Migration impact:** Do not auto-import workspace Python functions/pipelines
  into the capability registry.
- **Current repository compatibility:** Legacy extensions remain opt-in and
  explicitly outside this guarantee; whole-repository compliance is partial.

## ADR 12: Explicit Publish and Approval

- **Context:** Model-generated proposals and conversational agreement are ambiguous.
- **Decision:** Main Agent creates drafts or structured pending actions; a human
  entry reviews exact IDs/content and consumes a bounded confirmation ticket.
- **Alternatives considered:** Silent publish/enable; treating "OK" as high-risk
  consent; a global force/yes bypass.
- **Consequences:** Extra review step, live checks and stale-preview rejection.
- **Migration impact:** Durable ApprovalRequest remains separate from process-local
  human confirmation tickets.
- **Current repository compatibility:** Rich prompts and existing REST/UI previews
  share the same application authorizers; restart requires fresh preview.

## ADR 13: Shared Application Services

- **Context:** Agent, Web and CLI must expose one authoritative state.
- **Decision:** New entries call PlatformApplication and existing domain services;
  ORM/SQL remain in repository/query adapters.
- **Alternatives considered:** UI-only authorization; duplicate API/CLI business
  implementations; direct entry-point ORM access.
- **Consequences:** Common concurrency, policy, version and confirmation behavior.
- **Migration impact:** New platform queries get a read Protocol/SQLite adapter.
- **Current repository compatibility:** Existing REST prefix, collection shape,
  Typer/Rich style, static Next.js export and legacy routes are preserved. Legacy
  ORM entry migration and missing CLI/Agent control surfaces are deferred.

## ADR 14: Connection Reference Instead of Embedded Secrets

- **Context:** Versions, workflow definitions, receipts and logs are durable or
  inspectable; embedding credentials there is inappropriate.
- **Decision:** Declare named connection references; use existing keyring/
  environment credential references. Reject structured credential fields.
- **Alternatives considered:** Inline API keys, passwords or executable connection
  initialization in definitions.
- **Consequences:** A reference is not permission or an enabled external adapter.
  Arbitrary free-text secret detection is not guaranteed.
- **Migration impact:** New payload validation; no automatic historical secret scan.
- **Current repository compatibility:** Existing `identity_ref` remains usable.
  Connection-backed capability execution stays denied until an explicit adapter
  milestone supplies resolution, gates and reconciliation.

## ADR 15: Proactive Service Revision Snapshot

- **Context:** Service preferences/target/mapping can change while a run waits.
- **Decision:** Persist the admitted service identity/revision/configuration in
  invocation context. Later edits affect future admission only.
- **Alternatives considered:** Reload current service configuration at every node;
  mutate the execution when a service is edited.
- **Consequences:** Explain/history can show the actual admitted decision context.
  Live user/platform policy still rechecks; the snapshot cannot preserve revoked
  authority.
- **Migration impact:** JSON snapshot on the existing execution root and a scoped
  service-history index; optimistic service revision updates.
- **Current repository compatibility:** Per-service/global control stops future
  admission without rewriting existing executions or dependency locks.
