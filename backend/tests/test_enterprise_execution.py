import asyncio
import json

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from shovel.capability.domain import (
    CapabilityContent,
    ConnectionRequirement,
    DependencyLock,
    FunctionContract,
    FunctionImplementation,
    Permission,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.authentication_repository import SQLiteAuthenticationRepository
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.execution_control_repository import SQLiteExecutionControlRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.interaction_repository import SQLiteInteractionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.enterprise_execution import AuthenticationCompletion, ProtectionAudit
from shovel.db.protection_audit import SQLitePolicyAudit
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.db.workflow_repository import SQLiteWorkflowRepository
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import ConnectionBinding
from shovel.enterprise.execution import EnterpriseReadHandler, EnterpriseReadPlan
from shovel.enterprise.service import ConnectionService
from shovel.enterprise.transport import ProviderRequest
from shovel.execution.control import ExecutionWakeupService, RecoveryService
from shovel.execution.domain import ExecutionState, ExecutorResult
from shovel.execution.engine import ExecutionEngine
from shovel.identity.domain import (
    AuthContext,
    AuthenticationResolution,
    BrokerType,
    IdentityBinding,
    IdentityDraft,
)
from shovel.identity.errors import (
    AuthenticationRequiredError,
    ClaimsChallengeError,
    TokenAudienceMismatchError,
    WrongTenantError,
)
from shovel.identity.profiles import IdentityService
from shovel.identity.service import AuthenticationService
from shovel.protection.domain import (
    DataAction,
    DependencyKind,
    Lineage,
    LineageEdge,
    ProtectedContent,
    ProtectedContentRef,
    SourceBinding,
    SourceContentRef,
    content_digest,
)
from shovel.protection.persistence import SourceChange
from shovel.protection.policy import ProtectionPolicy
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import SecurityDenied, UserSecurityPolicy
from shovel.security.services import ApprovalService, AuthorizationService, PolicyEvaluator, SecurityGate
from shovel.workflow.service import WorkflowService
from test_capability_registry import SCOPE, locked
from test_capability_registry import catalog as catalog
from test_data_protection_contracts import metadata
from test_enterprise_connection_contracts import manifest, resource
from test_enterprise_connections import draft as connection_draft
from test_enterprise_identity_contracts import TENANT, token_request
from test_execution_engine import Kernel, invocation, publish, reference
from test_execution_engine import kernel as kernel
from test_protection_policy import reviewed_context, state_for
from test_security_core import Human

BODY = {"text": "ENTERPRISE-BODY-SENTINEL"}
TOKEN = "ENTERPRISE-TOKEN-SENTINEL"


class FixturePermissions:
    enabled = True
    require_approval = False

    def __init__(self, permission):
        self.permission = permission

    async def load(self, scope):
        assert scope == SCOPE
        return UserSecurityPolicy(
            user_grants=(self.permission,) if self.enabled else (),
            platform_constraints=(self.permission,),
            require_approval=self.require_approval,
        )


class FixtureConnectionPolicy(PolicyEvaluator):
    """Only this isolated composition admits the exact synthetic installed connection."""

    def __init__(self, identifier):
        self.identifier = identifier

    def evaluate(self, content, risk, policy):
        if content.connection_requirements:
            assert content.connection_requirements == (
                ConnectionRequirement(name="source", connection_ref=self.identifier),
            )
            assert content.implementation.implementation_id == "demo.read"
        super().evaluate(content.model_copy(update={"connection_requirements": ()}), risk, policy)


class FixturePolicy:
    def __init__(self, flow):
        self.flow = flow
        self.allowed = {DataAction.READ, DataAction.DISPLAY}
        self.active = True

    def resolve(self, context, binding):
        assert context == self.flow.contexts[binding.execution_id]
        state = state_for(context, binding)
        now = self.flow.kernel.now[0]
        return state.model_copy(
            update={
                "captured_at": now - 1,
                "revalidate_at": now + 50,
                "execution_deadline": self.flow.executions[binding.execution_id].deadline_at,
                "rules": state.rules if binding.action in self.allowed else (),
                "sources": tuple(
                    item.model_copy(
                        update={
                            "active": self.active,
                            "snapshot": item.snapshot.model_copy(
                                update={"captured_at": now - 1, "revalidate_at": now + 50}
                            ),
                        }
                    )
                    for item in state.sources
                ),
            }
        )


class FixtureBroker:
    error = None
    before_return = None

    def __init__(self, flow):
        self.flow, self.calls, self.contexts = flow, 0, []

    async def acquire(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error(correlation_id=request.correlation_id)
        if self.before_return is not None:
            await self.before_return()
        credential = AuthContext(request, SecretStr(TOKEN), expires_at=self.flow.kernel.now[0] + 60)
        self.contexts.append((request, credential))
        return credential


class FixtureTransport:
    before_response = None
    error = False

    def __init__(self, flow):
        self.flow, self.calls = flow, 0

    async def send(self, request, authentication, *, decode):
        authentication.consume(
            request.authentication,
            now=self.flow.kernel.now[0],
            transport=lambda secret: self._send(secret),
        )
        if self.before_response is not None:
            await self.before_response()
        if self.error:
            raise RuntimeError(f"{TOKEN}: {BODY}")
        return decode(json.dumps(BODY).encode())

    def _send(self, secret):
        assert secret.get_secret_value() == TOKEN
        self.calls += 1


class Flow:
    source_version = "v1"
    claims_ref = None
    classification = "internal"
    display_error = False

    def __init__(self, kernel):
        self.kernel = kernel
        self.frames, self.contexts, self.executions = [], {}, {}
        self.broker = FixtureBroker(self)
        self.transport = FixtureTransport(self)
        self.data_policy = FixturePolicy(self)
        self.audit = SQLitePolicyAudit()
        self.protection = SQLiteProtectionRepository(
            kernel.catalog.sessions,
            ProtectionPolicy(self.audit, provider=self.data_policy, clock=lambda: kernel.now[0]),
            source_authority=self,
            clock=lambda: kernel.now[0],
        )
        self.authentication = AuthenticationService(self, self.broker, clock=lambda: kernel.now[0])

    async def resolve(self, value):
        # AuthenticationService resolves the same explicit request against current metadata.
        if not hasattr(value, "snapshot"):
            execution = self.executions[value.execution_id]
            plan = await self.plan(execution)
            return AuthenticationResolution(
                approved_request=plan.request.authentication,
                profile=plan.identity,
                deadline_at=execution.deadline_at,
            )
        return await self.plan(value)

    async def plan(self, execution):
        self.executions[execution.id] = execution
        identity = (await self.identities.get(SCOPE, self.identity_id)).profile.model_copy(
            update={"enabled": True, "broker_type": BrokerType.PYTHON_WAM}
        )
        profile = (await self.connections.get(SCOPE, self.connection_id)).profile.model_copy(
            update={"enabled": True, "status": "validated"}
        )
        binding = IdentityBinding(
            workspace_id=SCOPE.workspace_id,
            user_id=SCOPE.user_id,
            identity_profile_id=identity.id,
            identity_revision=identity.revision,
            principal_ref="fixture-principal",
            tenant_id=TENANT,
        )
        request = token_request(
            request_id=f"{execution.id}.{execution.attempts}",
            execution_id=execution.id,
            correlation_id=execution.correlation_id,
            connection_profile_id=profile.id,
            connection_revision=profile.revision,
            manifest_digest=self.manifest.content_hash,
            identity=binding,
            claims_challenge_ref=self.claims_ref,
        )
        connection = ConnectionBinding(
            connection_profile_id=profile.id,
            connection_revision=profile.revision,
            provider_id=profile.provider_id,
            provider_version=self.manifest.provider_version,
            manifest_digest=self.manifest.content_hash,
            identity_profile_id=identity.id,
            tenant_id=TENANT,
            endpoint=profile.endpoint,
        )
        source = SourceBinding(
            resource=resource(connection_profile_id=profile.id, source_version=self.source_version),
            identity_profile_id=identity.id,
            principal_ref=binding.principal_ref,
            source_generation=0,
        )
        return EnterpriseReadPlan(
            request=ProviderRequest(
                operation_id="demo.read",
                connection=connection,
                authentication=request,
                resource=source.resource,
            ),
            profile=profile,
            manifest=self.manifest,
            identity=identity,
            source=source,
        )

    def decode(self, plan, response):
        body = json.loads(response)
        now = self.kernel.now[0]
        ctx = reviewed_context(sources=(plan.source,)).model_copy(
            update={
                "id": f"{plan.request.authentication.execution_id}.protection",
                "classification_id": self.classification,
                "expires_at": now + 100,
                "revalidate_at": now + 100,
            }
        )
        self.contexts[plan.request.authentication.execution_id] = ctx
        return ProtectedContent(
            body,
            metadata(
                reference=ProtectedContentRef(
                    reference=SourceContentRef(resource=plan.source.resource),
                    protection_context_ref=ctx.id,
                    content_digest=content_digest(body),
                    sources=ctx.sources,
                ),
                protection_context=ctx,
                provenance=Lineage(edges=(LineageEdge(source=plan.source, kind=DependencyKind.DATA),)),
                created_at=now,
            ),
        )

    def display(self, execution_id, value):
        self.frames.append((execution_id, value))
        if self.display_error:
            raise RuntimeError(f"{TOKEN}: {BODY}")

    def verify(self, change):
        assert change.scope == SCOPE and change.resource.connection_profile_id == self.connection_id

    async def revoke(self, execution):
        plan = await self.plan(execution)
        await self.protection.invalidate(
            SourceChange(
                event_id="verified-revocation",
                scope=SCOPE,
                resource=plan.source.resource,
                expected_generation=0,
                reason="source_revoked",
            )
        )

    async def invoke(self, *, request_key=None):
        request = invocation(self.version, request_key=request_key)
        request = request.model_copy(
            update={"context": request.context.model_copy(update={"permission_ceiling": (self.permission,)})}
        )
        return await self.kernel.invocations.invoke(request)

    async def run(self, execution):
        return await self.kernel.engine.run(SCOPE, execution.id)

    async def restart(self):
        await self.kernel.engine.close()
        self.requests = SQLiteAuthenticationRepository(
            self.kernel.catalog.sessions, clock=lambda: self.kernel.now[0]
        )
        self.kernel.engine = ExecutionEngine(
            self.kernel.invocations,
            self.kernel.engine._executors,
            clock=lambda: self.kernel.now[0],
            authentication_waits=self.requests,
            wakeups=self.wakeups,
        )


@pytest.fixture
async def flow(kernel: Kernel):
    flow = Flow(kernel)
    flow.identities = IdentityService(SQLiteIdentityRepository(kernel.catalog.sessions))
    identity = await flow.identities.create(
        SCOPE, IdentityDraft(display_name="Fixture", tenant_binding=TENANT)
    )
    flow.identity_id = identity.profile.id
    flow.manifest = manifest().model_copy(update={"function_implementations": ("demo.read",)})
    flow.connections = ConnectionService(
        SQLiteConnectionRepository(kernel.catalog.sessions),
        InstalledProviderCatalog((flow.manifest,), runtime_version="0.1.2"),
    )
    connection = await flow.connections.create(SCOPE, connection_draft(identity_profile_ref=flow.identity_id))
    flow.connection_id = connection.profile.id
    flow.permission = Permission(action="enterprise.read", resource=flow.connection_id)
    flow.permissions = FixturePermissions(flow.permission)
    flow.gate = SecurityGate(AuthorizationService(flow.permissions))
    flow.gate.policies = FixtureConnectionPolicy(flow.connection_id)
    kernel.invocations.security = flow.gate
    contract = FunctionContract(
        implementation_id="demo.read",
        required_permissions=(flow.permission,),
        connection_requirements=(ConnectionRequirement(name="source", connection_ref=flow.connection_id),),
    )
    kernel.catalog.registry._functions["demo.read"] = contract
    flow.version = await publish(
        kernel.catalog,
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id="demo.read"),
            permission_boundary=(flow.permission,),
            **contract.model_dump(exclude={"implementation_id", "retry_safe"}),
        ),
    )
    flow.requests = SQLiteAuthenticationRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0])
    control = SQLiteExecutionControlRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0])
    flow.wakeups = ExecutionWakeupService(control)
    kernel.engine.authentication_waits = flow.requests
    kernel.engine.wakeups = flow.wakeups
    kernel.functions._functions["demo.read"] = EnterpriseReadHandler(
        flow,
        flow.authentication,
        flow.transport,
        flow,
        flow.protection,
        flow,
        flow.gate,
    )
    return flow


async def assert_no_durable_body(flow):
    async with flow.kernel.catalog.sessions() as session:
        tables = await session.scalars(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        for table in tables:
            rows = (await session.execute(text(f'SELECT * FROM "{table}"'))).all()
            serialized = str(rows)
            assert TOKEN not in serialized and "ENTERPRISE-BODY-SENTINEL" not in serialized


async def test_real_engine_safe_whole_buffer_output_and_persistent_redacted_audit(flow):
    execution = await flow.invoke(request_key="first")
    completed = await flow.run(execution)
    assert completed.state == ExecutionState.SUCCEEDED
    assert completed.output == {"status": "displayed"}
    assert flow.frames == [(execution.id, BODY)]
    assert flow.broker.calls == flow.transport.calls == 1
    async with flow.kernel.catalog.sessions() as session:
        events = list(await session.scalars(select(ProtectionAudit.event)))
        assert {item["action"] for item in events} == {"read", "display"}
        assert all(item["outcome"] == "allow" for item in events)
    await assert_no_durable_body(flow)
    assert (await flow.invoke(request_key="first")).id == execution.id
    assert flow.transport.calls == 1


@pytest.mark.parametrize("action", [DataAction.READ, DataAction.DISPLAY])
async def test_policy_denial_in_actual_engine_emits_no_frame_or_persistent_body(flow, action):
    flow.data_policy.allowed.remove(action)
    execution = await flow.invoke()
    failed = await flow.run(execution)
    assert failed.state == ExecutionState.FAILED and failed.error == "data_use_denied"
    assert flow.frames == []
    async with flow.kernel.catalog.sessions() as session:
        events = list(await session.scalars(select(ProtectionAudit.event)))
        assert any(item["action"] == action.value and item["outcome"] == "deny" for item in events)
    await assert_no_durable_body(flow)


async def test_unknown_protection_never_reaches_display(flow):
    flow.classification = None
    failed = await flow.run(await flow.invoke())
    assert failed.error == "data_use_denied" and flow.frames == []
    await assert_no_durable_body(flow)


async def test_authentication_suspend_is_atomic_restartable_and_explicitly_resumed(flow):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    suspended = await flow.run(execution)
    assert suspended.wait_reason == "waiting_for_authentication"
    requests = await flow.requests.list(SCOPE)
    assert len(requests) == 1 and requests[0].state == "pending"
    assert suspended.deadline_at == requests[0].expires_at
    assert flow.transport.calls == 0 and flow.frames == []
    interactions = SQLiteInteractionRepository(flow.kernel.catalog.sessions, clock=lambda: flow.kernel.now[0])
    projection = (await interactions.list(SCOPE))[0]
    await interactions.dismiss(SCOPE, projection.id)
    assert (await flow.requests.get(SCOPE, requests[0].id)).state == "pending"
    assert await flow.wakeups.run_due() == 0
    await flow.restart()
    flow.broker.error = None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    completed = await flow.run(execution)
    assert completed.state == ExecutionState.SUCCEEDED and completed.deadline_at == suspended.deadline_at
    assert (await flow.requests.get(SCOPE, requests[0].id)).state == "satisfied"
    assert (await interactions.list(SCOPE))[0].state == "resolved"
    assert flow.broker.calls == 2 and flow.transport.calls == 1
    await assert_no_durable_body(flow)


async def test_repeated_silent_failure_does_not_poll_or_extend_deadline(flow):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    first = await flow.run(execution)
    await flow.kernel.engine.resume(SCOPE, execution.id)
    second = await flow.run(execution)
    assert second.state == ExecutionState.SUSPENDED and second.deadline_at == first.deadline_at
    requests = await flow.requests.list(SCOPE)
    assert sorted(item.state for item in requests) == ["cancelled", "pending"]
    assert flow.broker.calls == 2 and flow.transport.calls == 0 and not flow.frames


@pytest.mark.parametrize("action", ["cancel", "expire", "cancel_request"])
async def test_authentication_wait_cancel_and_timeout_cannot_resume_business(flow, action):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    suspended = await flow.run(execution)
    request = (await flow.requests.list(SCOPE))[0]
    if action == "cancel":
        await flow.kernel.engine.cancel(SCOPE, execution.id)
    elif action == "expire":
        flow.kernel.now[0] = suspended.deadline_at + 1
        await flow.wakeups.run_due()
    else:
        await flow.requests.cancel(SCOPE, request.id, execution_id=execution.id)
    with pytest.raises(RuntimeFailure):
        await flow.kernel.engine.resume(SCOPE, execution.id)
    assert (await flow.requests.get(SCOPE, request.id)).state in {"cancelled", "expired"}
    assert flow.transport.calls == 0 and not flow.frames


async def test_frozen_source_change_after_restart_is_not_a_new_implicit_read(flow):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    await flow.run(execution)
    await flow.restart()
    flow.source_version, flow.broker.error = "v2", None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    failed = await flow.run(execution)
    assert failed.state == ExecutionState.FAILED
    assert flow.broker.calls == 1 and flow.transport.calls == 0 and not flow.frames


@pytest.mark.parametrize("stage", ["before_auth", "during_auth", "during_transport"])
async def test_source_revocation_fences_every_release_and_late_result(flow, stage):
    execution = await flow.invoke()

    async def revoke():
        await flow.revoke(flow.executions[execution.id])

    if stage == "before_auth":
        await flow.revoke(execution)
    elif stage == "during_auth":
        flow.broker.before_return = revoke
    else:
        flow.transport.before_response = revoke
    failed = await flow.run(execution)
    assert failed.error == "data_use_denied" and not flow.frames
    assert flow.transport.calls == (1 if stage == "during_transport" else 0)
    await assert_no_durable_body(flow)


async def test_platform_permission_revocation_after_auth_blocks_transport(flow):
    async def revoke():
        flow.permissions.enabled = False

    flow.broker.before_return = revoke
    failed = await flow.run(await flow.invoke())
    assert failed.error == "security_denied"
    assert flow.transport.calls == 0 and not flow.frames


async def test_fresh_claims_binding_is_required_and_never_persisted(flow):
    flow.broker.error = ClaimsChallengeError
    execution = await flow.invoke()
    await flow.run(execution)
    request = (await flow.requests.list(SCOPE))[0]
    assert request.requires_fresh_challenge and request.request.claims_challenge_ref is None
    flow.broker.error, flow.claims_ref = None, "fresh-process-challenge"
    await flow.kernel.engine.resume(SCOPE, execution.id)
    assert (await flow.run(execution)).state == ExecutionState.SUCCEEDED
    async with flow.kernel.catalog.sessions() as session:
        assert await session.get(AuthenticationCompletion, request.id) is not None
    await assert_no_durable_body(flow)


async def test_audit_failure_never_releases_content_and_errors_are_safe(flow):
    engine = flow.kernel.catalog.sessions.kw["bind"]

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO protection_audit"):
            raise OperationalError(statement, parameters, RuntimeError(TOKEN))

    event.listen(engine.sync_engine, "before_cursor_execute", reject)
    try:
        failed = await flow.run(await flow.invoke())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject)
    assert failed.state == ExecutionState.FAILED and not flow.frames
    assert TOKEN not in failed.model_dump_json()
    assert flow.protection._gate.policy._issued == {}
    await assert_no_durable_body(flow)


async def test_output_failure_and_interrupted_release_intent_are_not_replayed(flow):
    flow.display_error = True
    execution = await flow.invoke()
    failed = await flow.run(execution)
    assert failed.state == ExecutionState.FAILED and len(flow.frames) == 1
    assert failed.control["enterprise_release_started"] is True
    next_execution = await flow.invoke()
    lease = await flow.kernel.engine.start(SCOPE, next_execution.id)
    await flow.kernel.repository.dispatch(lease, flow.gate)
    await flow.requests.prepare_output(lease)
    control = SQLiteExecutionControlRepository(flow.kernel.catalog.sessions, clock=lambda: flow.kernel.now[0])
    assert await RecoveryService(control, lambda _content: True).recover() == 1
    interrupted = await flow.kernel.repository.get(SCOPE, next_execution.id)
    assert interrupted.control["replay_safe"] is False
    assert await flow.wakeups.run_due() == 0
    with pytest.raises(RuntimeFailure, match="reconciliation"):
        await flow.kernel.engine.resume(SCOPE, next_execution.id)
    assert len(flow.frames) == 1
    await assert_no_durable_body(flow)


async def test_default_production_policy_still_denies_connection_backed_invocation(flow):
    flow.kernel.invocations.security = SecurityGate(AuthorizationService(flow.permissions))
    with pytest.raises(SecurityDenied, match="not enabled"):
        await flow.invoke()
    assert flow.broker.calls == flow.transport.calls == 0 and not flow.frames


async def test_completed_workflow_child_does_not_repeat_across_authentication_wait(flow):
    controls = WorkflowService(
        SQLiteWorkflowRepository(flow.kernel.catalog.sessions, clock=lambda: flow.kernel.now[0]),
        flow.gate,
    )
    flow.kernel.engine._workflow_context = controls.bind
    count = []
    flow.kernel.catalog.registry._functions["fixture.once"] = FunctionContract(
        implementation_id="fixture.once"
    )

    async def once(context):
        count.append(context.execution.id)
        return ExecutorResult(output={"status": "once"})

    flow.kernel.functions._functions["fixture.once"] = once
    first = await publish(
        flow.kernel.catalog,
        CapabilityContent(implementation=FunctionImplementation(implementation_id="fixture.once")),
    )
    parent = await publish(
        flow.kernel.catalog,
        CapabilityContent(
            implementation=WorkflowImplementation(
                nodes=(
                    WorkflowNode(id="input", kind="input"),
                    WorkflowNode(id="first", kind="function", capability=reference(first)),
                    WorkflowNode(id="enterprise", kind="function", capability=reference(flow.version)),
                    WorkflowNode(id="output", kind="output"),
                ),
                edges=(
                    WorkflowEdge(source="input", target="first"),
                    WorkflowEdge(source="first", target="enterprise"),
                    WorkflowEdge(source="enterprise", target="output"),
                ),
            ),
            required_permissions=(flow.permission,),
            permission_boundary=(flow.permission,),
            allowed_capabilities=(reference(first), reference(flow.version)),
            dependency_lock=DependencyLock(dependencies=(locked(first), locked(flow.version))),
        ),
    )
    request = invocation(parent)
    request = request.model_copy(
        update={"context": request.context.model_copy(update={"permission_ceiling": (flow.permission,)})}
    )
    execution = await flow.kernel.invocations.invoke(request)
    flow.broker.error = AuthenticationRequiredError
    assert (await flow.run(execution)).state == ExecutionState.SUSPENDED
    assert len(count) == 1 and not flow.frames
    assert await flow.wakeups.run_due() == 0
    flow.broker.error = None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    assert (await flow.run(execution)).state == ExecutionState.SUCCEEDED
    assert len(count) == 1 and len(flow.frames) == 1


async def test_migration_eighteen_preserves_pending_wait_and_old_hashes(flow):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    await flow.run(execution)
    before = (await flow.requests.list(SCOPE))[0]
    engine = flow.kernel.catalog.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE protection_audit")
        await connection.exec_driver_sql("DROP TABLE authentication_completion")
        await connection.exec_driver_sql("DELETE FROM schema_migration WHERE version=18")
    await upgrade_database(engine)
    await upgrade_database(engine)
    assert (await flow.requests.list(SCOPE))[0] == before
    assert (
        await flow.kernel.catalog.registry.get_version(
            SCOPE,
            flow.version.capability_id,
            flow.version.number,
        )
    ).content_hash == flow.version.content_hash


@pytest.mark.parametrize("committed", [False, True])
async def test_caller_cancellation_during_authentication_suspend_never_strands_execution(
    flow,
    monkeypatch,
    committed,
):
    entered, never = asyncio.Event(), asyncio.Event()
    original = flow.requests.suspend

    async def delayed(lease, wait):
        if committed:
            await original(lease, wait)
        entered.set()
        await never.wait()
        return await original(lease, wait)

    monkeypatch.setattr(flow.requests, "suspend", delayed)
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    task = asyncio.create_task(flow.run(execution))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await flow.kernel.repository.get(SCOPE, execution.id)).state == ExecutionState.CANCELLED
    assert all(item.state == "cancelled" for item in await flow.requests.list(SCOPE))
    assert flow.transport.calls == 0 and not flow.frames


@pytest.mark.parametrize("changed", ["identity", "connection"])
async def test_configuration_revision_change_during_authentication_blocks_business(flow, changed):
    async def update():
        if changed == "identity":
            await flow.identities.update(
                SCOPE,
                flow.identity_id,
                IdentityDraft(display_name="Changed", tenant_binding=TENANT),
                expected_revision=1,
            )
        else:
            await flow.connections.update(
                SCOPE,
                flow.connection_id,
                connection_draft(identity_profile_ref=flow.identity_id),
                expected_revision=1,
            )

    flow.broker.before_return = update
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED
    assert flow.transport.calls == 0 and not flow.frames


@pytest.mark.parametrize("changed", ["permission", "approval", "protection"])
async def test_live_policy_change_during_read_prevents_output(flow, changed):
    async def revoke():
        if changed == "permission":
            flow.permissions.enabled = False
        elif changed == "approval":
            flow.permissions.require_approval = True
        else:
            flow.data_policy.active = False

    flow.transport.before_response = revoke
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED and not flow.frames
    assert flow.transport.calls == 1
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("error", [WrongTenantError, TokenAudienceMismatchError])
async def test_incorrect_authentication_configuration_is_not_a_resumable_user_prompt(flow, error):
    flow.broker.error = error
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED and failed.error == error.code
    assert await flow.requests.list(SCOPE) == []
    assert flow.transport.calls == 0 and not flow.frames


async def test_original_deadline_applies_to_broker_and_never_extends_on_authentication(flow):
    async def expire():
        flow.kernel.now[0] = next(iter(flow.executions.values())).deadline_at + 1

    flow.broker.before_return = expire
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED and failed.error == "authentication_deadline_exceeded"
    assert not flow.frames and flow.transport.calls == 0


@pytest.mark.parametrize("mode", ["oversized", "repeated", "bypassed", "control_only", "wrong_source"])
async def test_untrusted_or_unbounded_decoder_result_cannot_release(flow, monkeypatch, mode):
    original_send = flow.transport.send
    original_decode = flow.decode

    async def send(request, authentication, *, decode):
        if mode == "bypassed":
            return await original_send(request, authentication, decode=lambda _: BODY)

        def malformed(response):
            if mode == "oversized":
                return decode(b"x" * 262145)
            if mode == "repeated":
                decode(response)
            return decode(response)

        return await original_send(request, authentication, decode=malformed)

    def malformed_decode(plan, response):
        if mode == "wrong_source":
            plan = plan.model_copy(
                update={
                    "source": plan.source.model_copy(
                        update={"resource": plan.source.resource.model_copy(update={"source_version": "v2"})}
                    )
                }
            )
        content = original_decode(plan, response)
        if mode == "control_only":
            return ProtectedContent(
                BODY,
                content.metadata.model_copy(
                    update={
                        "provenance": Lineage(
                            edges=(LineageEdge(source=plan.source, kind=DependencyKind.CONTROL),)
                        )
                    }
                ),
            )
        return content

    monkeypatch.setattr(flow.transport, "send", send)
    monkeypatch.setattr(flow, "decode", malformed_decode)
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED and not flow.frames
    await assert_no_durable_body(flow)


async def test_missing_fresh_claims_does_not_consume_a_completed_authentication_boolean(flow):
    flow.broker.error = ClaimsChallengeError
    execution = await flow.invoke()
    await flow.run(execution)
    flow.broker.error = None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    assert (await flow.run(execution)).state == ExecutionState.FAILED
    assert flow.transport.calls == 0 and not flow.frames


async def test_adapter_exception_details_are_not_logged_or_persisted(flow, capsys):
    flow.transport.error = True
    failed = await flow.run(await flow.invoke())
    assert failed.error == "executor_failed" and not flow.frames
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "ENTERPRISE-BODY-SENTINEL" not in captured.out + captured.err
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("table", ["authentication_completion", "protection_audit"])
@pytest.mark.parametrize("statement", ["DELETE FROM", "UPDATE"])
async def test_completion_and_audit_facts_are_immutable_and_retained(flow, table, statement):
    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    await flow.run(execution)
    flow.broker.error = None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    assert (await flow.run(execution)).state == ExecutionState.SUCCEEDED
    query = f"{statement} {table}"
    if statement == "UPDATE":
        column = "operation_digest" if table == "authentication_completion" else "workspace_id"
        query += f" SET {column} = 'changed'"
    async with flow.kernel.catalog.sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(text(query))
        await session.rollback()


async def test_existing_approval_gate_precedes_authentication_and_remains_valid_at_release(flow):
    flow.permissions.require_approval = True
    execution = await flow.invoke()
    waiting = await flow.run(execution)
    assert waiting.state == ExecutionState.SUSPENDED and waiting.approval_request_id
    assert flow.broker.calls == flow.transport.calls == 0 and not flow.frames
    approvals = ApprovalService(
        SQLiteApprovalRepository(flow.kernel.catalog.sessions, clock=lambda: flow.kernel.now[0]),
        flow.gate,
        authorizer=Human(),
    )
    await approvals.approve(
        SCOPE,
        execution.id,
        waiting.approval_request_id,
        user_id=SCOPE.user_id,
    )
    await flow.kernel.engine.resume(SCOPE, execution.id)
    completed = await flow.run(execution)
    assert completed.state == ExecutionState.SUCCEEDED and len(flow.frames) == 1
    assert flow.broker.calls == flow.transport.calls == 1


@pytest.mark.parametrize("table", ["authentication_request", "interaction"])
async def test_authentication_suspend_and_projection_roll_back_as_one_unit(flow, table):
    engine = flow.kernel.catalog.sessions.kw["bind"]

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith(f"INSERT INTO {table}"):
            raise OperationalError(statement, parameters, RuntimeError(TOKEN))

    flow.broker.error = AuthenticationRequiredError
    execution = await flow.invoke()
    event.listen(engine.sync_engine, "before_cursor_execute", reject)
    try:
        failed = await flow.run(execution)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject)
    assert failed.state == ExecutionState.FAILED and failed.error == "authentication_wait_failed"
    assert await flow.requests.list(SCOPE) == []
    assert (await flow.kernel.repository.attempts(SCOPE, execution.id))[0].outcome == ExecutionState.FAILED
    assert flow.transport.calls == 0 and not flow.frames
    await assert_no_durable_body(flow)


async def test_committed_allow_decision_is_not_a_release_receipt_or_reusable_grant(flow):
    engine = flow.kernel.catalog.sessions.kw["bind"]

    def revoke(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO protection_audit"):
            flow.data_policy.active = False

    event.listen(engine.sync_engine, "after_cursor_execute", revoke)
    try:
        failed = await flow.run(await flow.invoke())
    finally:
        event.remove(engine.sync_engine, "after_cursor_execute", revoke)
    assert failed.error == "data_use_denied" and not flow.frames
    async with flow.kernel.catalog.sessions() as session:
        audits = list(await session.scalars(select(ProtectionAudit.event)))
    assert len(audits) == 1 and audits[0]["outcome"] == "allow"
    assert flow.protection._gate.policy._issued == {}
