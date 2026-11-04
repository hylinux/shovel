"""Synthetic ES-10 composition, never imported or installed by the production Runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Never

from pydantic import JsonValue, SecretStr
from scripts.synthetic_storage import synthetic_governance
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    CapabilityScope,
    CapabilityVersion,
    ConnectionRequirement,
    FunctionContract,
    FunctionImplementation,
    Permission,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db.authentication_repository import SQLiteAuthenticationRepository
from shovel.db.capability_repository import SQLiteCapabilityRepository
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.execution_control_repository import SQLiteExecutionControlRepository
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.enterprise_execution import ProtectionAudit
from shovel.db.protection_audit import SQLitePolicyAudit
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.db.unit_of_work import sqlite_write
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import (
    Cloud,
    ConnectionBinding,
    ConnectionDraft,
    EnterpriseConnectionProfile,
    EnterpriseResourceRef,
    ProviderEnvironment,
    ProviderManifest,
)
from shovel.enterprise.execution import EnterpriseReadHandler, EnterpriseReadPlan
from shovel.enterprise.service import ConnectionService
from shovel.enterprise.transport import ProviderRequest
from shovel.execution.control import ExecutionWakeupService
from shovel.execution.domain import (
    Execution,
    ExecutionBudget,
    ExecutionState,
    ExecutorResult,
    Invocation,
    InvocationContext,
)
from shovel.execution.engine import ExecutionEngine
from shovel.execution.executors import AgentExecutor, FunctionExecutor, SkillExecutor, WorkflowExecutor
from shovel.execution.invocation import InvocationService
from shovel.execution.ports import ExecutorContext
from shovel.identity.domain import (
    AuthContext,
    AuthenticationResolution,
    AuthenticationState,
    BrokerType,
    IdentityBinding,
    IdentityDraft,
    IdentityProfile,
    TokenRequest,
)
from shovel.identity.errors import AuthenticationRequiredError
from shovel.identity.profiles import IdentityService
from shovel.identity.service import AuthenticationService
from shovel.protection.domain import (
    AuthorizationSnapshot,
    ContentRight,
    DataAction,
    DataDestination,
    DataUseBinding,
    DependencyKind,
    Lineage,
    LineageEdge,
    ModelBinding,
    PolicyOutcome,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionContext,
    SourceBinding,
    SourceContentRef,
    content_digest,
)
from shovel.protection.policy import DataRule, PolicyState, ProtectionPolicy, SourceAuthorization
from shovel.protection.service import ProtectionService
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import RiskAssessment, SecurityDenied, UserSecurityPolicy
from shovel.security.services import AuthorizationService, PolicyEvaluator, SecurityGate

SCOPE = CapabilityScope(workspace_id="synthetic-workspace", user_id="synthetic-user")
TENANT = "00000000-0000-0000-0000-000000000001"
POLICY_VERSION = content_digest("synthetic-policy-v1")
CREDENTIAL = "SYNTHETIC-CREDENTIAL-NOT-A-REAL-TOKEN"
SOURCE_TEXT = "Synthetic document: the fictional project is on schedule."
DERIVED_TEXT = "Synthetic summary: on schedule."
READ, DERIVE = "sample.enterprise_read", "sample.enterprise_derive"


def now() -> float:
    return 1000.0


class SyntheticBroker:
    ready = True
    calls = 0

    async def availability(self, profile: IdentityProfile) -> AuthenticationState:
        return AuthenticationState.READY_SILENT if self.ready else AuthenticationState.INTERACTION_REQUIRED

    async def cancel(self, request_id: str) -> None:
        self.ready = False

    async def disconnect(self, profile: IdentityProfile) -> None:
        self.ready = False

    async def acquire(self, request: TokenRequest) -> AuthContext:
        self.calls += 1
        if not self.ready:
            raise AuthenticationRequiredError(correlation_id=request.correlation_id)
        return AuthContext(request, SecretStr(CREDENTIAL), expires_at=now() + 120)


class SyntheticProvider:
    """Fixed in-memory bytes; no HTTP client, SDK, environment or user configuration."""

    def __init__(self, identity: IdentityProfile, profile: EnterpriseConnectionProfile) -> None:
        self.identity, self.profile = identity, profile
        self.executions: dict[str, Execution] = {}
        self.plans: dict[str, EnterpriseReadPlan] = {}
        self.transport_calls = 0
        self.manifest = ProviderManifest(
            provider_id="synthetic",
            provider_version="1.0",
            supported_connection_types=("documents",),
            environments=(
                ProviderEnvironment(
                    cloud=Cloud.AZURE_GLOBAL,
                    environment="sample",
                    authority_origins=("https://login.example.invalid",),
                    endpoints=("https://documents.example.invalid",),
                    audiences=("api://synthetic",),
                    permitted_scopes=("api://synthetic/read",),
                ),
            ),
            function_implementations=(READ, DERIVE),
            classifications_emitted=("sample.internal",),
            protection_metadata_supported=("source", "rights"),
            availability_probe="synthetic.available",
            health_check="synthetic.health",
            minimum_runtime_version="0.1.2",
        )

    async def resolve(self, execution: Execution) -> EnterpriseReadPlan:
        self.executions[execution.id] = execution
        implementation = execution.snapshot.capability.content.implementation
        if not isinstance(implementation, FunctionImplementation):
            raise SecurityDenied("The sample accepts only its installed Functions.")
        identity = IdentityBinding(
            workspace_id=SCOPE.workspace_id,
            user_id=SCOPE.user_id,
            identity_profile_id=self.identity.id,
            identity_revision=self.identity.revision,
            principal_ref="synthetic-principal",
            tenant_id=TENANT,
        )
        authentication = TokenRequest(
            request_id=f"{execution.id}.{execution.attempts}",
            execution_id=execution.id,
            connection_profile_id=self.profile.id,
            connection_revision=self.profile.revision,
            provider_id=self.manifest.provider_id,
            provider_version=self.manifest.provider_version,
            manifest_digest=self.manifest.content_hash,
            identity=identity,
            authority=f"https://login.example.invalid/{TENANT}",
            audience="api://synthetic",
            allowed_scopes=("api://synthetic/read",),
            correlation_id=execution.correlation_id,
        )
        source = SourceBinding(
            resource=EnterpriseResourceRef(
                provider_id="synthetic",
                connection_profile_id=self.profile.id,
                resource_type="document",
                resource_id="fictional-document",
                tenant_id=TENANT,
                source_version="v1",
            ),
            identity_profile_id=self.identity.id,
            principal_ref=identity.principal_ref,
            source_generation=0,
        )
        plan = EnterpriseReadPlan(
            request=ProviderRequest(
                operation_id=implementation.implementation_id,
                connection=ConnectionBinding(
                    connection_profile_id=self.profile.id,
                    connection_revision=self.profile.revision,
                    provider_id="synthetic",
                    provider_version="1.0",
                    manifest_digest=self.manifest.content_hash,
                    identity_profile_id=self.identity.id,
                    tenant_id=TENANT,
                    endpoint=self.profile.endpoint,
                ),
                authentication=authentication,
                resource=source.resource,
            ),
            profile=self.profile,
            identity=self.identity,
            manifest=self.manifest,
            source=source,
        )
        self.plans[execution.id] = plan
        return plan

    async def send(
        self,
        request: ProviderRequest,
        authentication: AuthContext,
        *,
        decode: Callable[[bytes], ProtectedContent[JsonValue]],
    ) -> ProtectedContent[JsonValue]:
        if request.operation_id not in {READ, DERIVE} or request.parameter_refs:
            raise SecurityDenied("The synthetic transport does not accept arbitrary operations.")
        self.transport_calls += 1
        return authentication.consume(
            request.authentication,
            now=now(),
            transport=lambda _credential: decode(json.dumps(SOURCE_TEXT).encode()),
        )

    def decode(self, plan: EnterpriseReadPlan, response: bytes) -> ProtectedContent[JsonValue]:
        if response != json.dumps(SOURCE_TEXT).encode():
            raise RuntimeFailure("The sample decoder accepts only its fixed synthetic response.")
        context = ProtectionContext(
            id=f"sample-{plan.request.authentication.execution_id}",
            sources=(plan.source,),
            classification_id="sample.internal",
            protection_status="unprotected",
            encrypted=False,
            rights=(ContentRight.READ, ContentRight.DISPLAY, ContentRight.TRANSFORM),
            policy_source_ref="synthetic-policy",
            policy_version=POLICY_VERSION,
            dlp_applicable=False,
            retention_policy_ref="synthetic-transient-retention",
            expires_at=now() + 120,
            revalidate_at=now() + 120,
        )
        metadata = ProtectedContentMetadata(
            reference=ProtectedContentRef(
                reference=SourceContentRef(resource=plan.source.resource),
                protection_context_ref=context.id,
                content_digest=content_digest(SOURCE_TEXT),
                sources=context.sources,
            ),
            protection_context=context,
            provenance=Lineage(edges=(LineageEdge(source=plan.source, kind=DependencyKind.DATA),)),
            authorization_snapshot_refs=("synthetic-snapshot-0",),
            created_at=now(),
            lifecycle_state="active",
        )
        return ProtectedContent(SOURCE_TEXT, metadata)


class SyntheticAuthenticationResolver:
    def __init__(self, provider: SyntheticProvider) -> None:
        self.provider = provider

    async def resolve(self, request: TokenRequest) -> AuthenticationResolution:
        execution = self.provider.executions[request.execution_id]
        plan = await self.provider.resolve(execution)
        if execution.deadline_at is None:
            raise RuntimeFailure("The sample requires a running Execution deadline.")
        return AuthenticationResolution(
            approved_request=plan.request.authentication,
            profile=plan.identity,
            deadline_at=execution.deadline_at,
        )


class SyntheticPolicy:
    display_allowed = True

    def __init__(self, provider: SyntheticProvider, broker: SyntheticBroker) -> None:
        self.provider, self.broker = provider, broker

    def resolve(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyState:
        execution = self.provider.executions[binding.execution_id]
        if execution.deadline_at is None:
            raise RuntimeFailure("The sample requires a running Execution deadline.")
        plan = self.provider.plans[binding.execution_id]
        if (
            set(context.sources) != {plan.source}
            or binding.identities != (plan.request.authentication.identity,)
            or binding.connections != (plan.request.connection,)
        ):
            raise SecurityDenied("The sample policy does not authorize other resources.")
        allowed = binding.action in {DataAction.READ, DataAction.DISPLAY, DataAction.TRANSFORM}
        allowed = allowed and (binding.action != DataAction.DISPLAY or self.display_allowed)
        sources = tuple(
            SourceAuthorization(
                source=source,
                active=True,
                authentication_valid=self.broker.ready,
                snapshot=AuthorizationSnapshot(
                    id=f"synthetic-snapshot-{index}",
                    workspace_id=SCOPE.workspace_id,
                    user_id=SCOPE.user_id,
                    principal_ref=source.principal_ref,
                    identity_profile_id=self.provider.identity.id,
                    identity_revision=self.provider.identity.revision,
                    tenant_id=TENANT,
                    resource_scope=(source.resource,),
                    decision="allow",
                    captured_at=now(),
                    policy_version=POLICY_VERSION,
                    freshness="fresh",
                    revalidate_at=now() + 120,
                ),
            )
            for index, source in enumerate(context.sources)
        )
        return PolicyState(
            context=context,
            binding=binding,
            classifications=("sample.internal",),
            sources=sources,
            rules=(
                DataRule(
                    id="synthetic-rule",
                    classifications=("sample.internal",),
                    action=binding.action,
                    destination=binding.destination,
                    outcome=PolicyOutcome.ALLOW,
                    retention_policy_refs=("synthetic-transient-retention",),
                ),
            )
            if allowed
            else (),
            captured_at=now(),
            revalidate_at=now() + 120,
            execution_deadline=execution.deadline_at,
        )


class SyntheticPermissions:
    def __init__(self, permission: Permission) -> None:
        self.permission = permission

    async def load(self, scope: CapabilityScope) -> UserSecurityPolicy:
        if scope != SCOPE:
            raise SecurityDenied("The sample has only its temporary synthetic owner.")
        return UserSecurityPolicy(user_grants=(self.permission,), platform_constraints=(self.permission,))


class SyntheticConnectionPolicy(PolicyEvaluator):
    def __init__(self, requirement: ConnectionRequirement) -> None:
        self.requirement = requirement

    def evaluate(self, content: CapabilityContent, risk: RiskAssessment, policy: UserSecurityPolicy) -> None:
        if (
            content.connection_requirements != (self.requirement,)
            or not isinstance(content.implementation, FunctionImplementation)
            or content.implementation.implementation_id not in {READ, DERIVE}
        ):
            raise SecurityDenied("Only the explicitly installed synthetic Functions are admitted.")
        super().evaluate(content.model_copy(update={"connection_requirements": ()}), risk, policy)


class SyntheticPublisher:
    def __init__(self, contracts: tuple[FunctionContract, ...]) -> None:
        self.hashes = {sample_content(contract).content_hash for contract in contracts}

    async def authorize(
        self,
        scope: CapabilityScope,
        action: Literal["publish", "availability"],
        capability_id: str,
        version: int | None,
        content_hash: str,
    ) -> None:
        if scope != SCOPE or action != "publish" or version != 1 or content_hash not in self.hashes:
            raise SecurityDenied("The sample publisher only approves its first synthetic versions.")


def sample_content(contract: FunctionContract) -> CapabilityContent:
    return CapabilityContent(
        implementation=FunctionImplementation(implementation_id=contract.implementation_id),
        permission_boundary=contract.required_permissions,
        **contract.model_dump(exclude={"implementation_id", "retry_safe"}),
    )


class Sample:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: SyntheticProvider,
        display: Callable[[JsonValue], None],
    ) -> None:
        self.sessions, self.provider, self.display_consumer = sessions, provider, display
        self.broker = SyntheticBroker()
        self.frames = 0
        self.state = SyntheticPolicy(provider, self.broker)
        self.audit = SQLitePolicyAudit()
        self.policy = ProtectionPolicy(self.audit, provider=self.state, clock=now)
        self.protection = SQLiteProtectionRepository(
            sessions,
            self.policy,
            clock=now,
            governance=synthetic_governance(
                SCOPE,
                now,
                retention_policy_refs=("synthetic-transient-retention",),
                protection_policy_refs=("synthetic-policy",),
            )[0],
        )
        self.authentication = AuthenticationService(
            SyntheticAuthenticationResolver(provider), self.broker, clock=now
        )
        self.permission = Permission(action="enterprise.read", resource=provider.profile.id)
        self.requirement = ConnectionRequirement(name="source", connection_ref=provider.profile.id)
        self.gate = SecurityGate(AuthorizationService(SyntheticPermissions(self.permission)))
        self.gate.policies = SyntheticConnectionPolicy(self.requirement)
        self.requests = SQLiteAuthenticationRepository(sessions, clock=now)
        self.derived_metadata: ProtectedContentMetadata | None = None

    def display(self, execution_id: str, value: JsonValue) -> None:
        if execution_id not in self.provider.executions:
            raise SecurityDenied("Unknown sample execution.")
        self.display_consumer(value)
        self.frames += 1

    def binding(
        self,
        execution: Execution,
        plan: EnterpriseReadPlan,
        content: ProtectedContent[JsonValue],
        action: DataAction,
        destination: DataDestination,
        *,
        model: ModelBinding | None = None,
    ) -> DataUseBinding:
        metadata = content.metadata
        return DataUseBinding(
            execution_id=execution.id,
            step_id=f"sample.{action.value}",
            actor_id=SCOPE.user_id,
            identities=(plan.request.authentication.identity,),
            connections=(plan.request.connection,),
            sources=metadata.protection_context.sources,
            protection_context_ref=metadata.protection_context.id,
            protection_context_digest=metadata.protection_context.content_hash,
            content_digest=metadata.reference.content_digest,
            action=action,
            destination=destination,
            model=model,
            policy_version=POLICY_VERSION,
        )

    async def derive(self, context: ExecutorContext) -> ExecutorResult:
        if context.authentication is None or context.lease is None:
            raise SecurityDenied("The existing Engine lifecycle is required.")
        plan = await self.provider.resolve(context.execution)
        plan.validate_execution(context.execution)
        await context.authentication.bind_operation(plan.digest)
        credential = await self.authentication.authenticate(plan.request.authentication)
        try:
            await context.authentication.authenticated(plan.request.authentication, credential, plan.digest)
            content = await self.provider.send(
                plan.request,
                credential,
                decode=lambda response: self.provider.decode(plan, response),
            )
        finally:
            credential.close()
        await self.protection.check_execution(context.lease, self.gate)
        await self.protection.check_sources(SCOPE, (plan.source,))
        transform = self.binding(
            context.execution, plan, content, DataAction.TRANSFORM, DataDestination.WORKFLOW_STATE
        )
        service = ProtectionService(self.policy, clock=now)
        # Pure, fixed in-memory computation; no body leaves before audit commit.
        async with sqlite_write(self.sessions) as session:
            with self.audit.bind(session, SCOPE, context.execution.id):
                derived = service.derive(
                    ((content, transform, DependencyKind.DATA),),
                    "sample.summary",
                    lambda _values: DERIVED_TEXT,
                )
            await session.commit()
        self.derived_metadata = derived.metadata
        embed = self.binding(
            context.execution,
            plan,
            derived,
            DataAction.EMBED,
            DataDestination.ENTERPRISE_MODEL,
            model=ModelBinding(
                model_profile_id="unapproved-synthetic-embedder",
                revision=1,
                provider="synthetic",
                model_name="never-called",
                endpoint="https://models.example.invalid",
                environment="sample",
            ),
        )
        async with sqlite_write(self.sessions) as session:
            with self.audit.bind(session, SCOPE, context.execution.id):
                decision = self.policy.evaluate(derived.metadata.protection_context, embed)
            await session.commit()
        if decision.outcome != PolicyOutcome.DENY:
            self.policy.revoke(decision.id)
            raise RuntimeFailure("The sample must not authorize an unapproved embedding sink.")
        return ExecutorResult(output={})

    async def scenarios(self) -> dict[str, JsonValue]:
        contracts = tuple(
            FunctionContract(
                implementation_id=identifier,
                required_permissions=(self.permission,),
                connection_requirements=(self.requirement,),
            )
            for identifier in (READ, DERIVE)
        )
        registry = CapabilityRegistry(SQLiteCapabilityRepository(self.sessions), functions=contracts)
        capabilities = CapabilityService(registry, authorizer=SyntheticPublisher(contracts))
        versions = {}
        for contract in contracts:
            content = sample_content(contract)
            draft = await capabilities.create_draft(
                SCOPE, CapabilityKind.FUNCTION, contract.implementation_id, content
            )
            versions[contract.implementation_id] = await capabilities.publish(
                SCOPE,
                draft.capability_id,
                draft.number,
                expected_revision=draft.revision,
            )
        invocations = InvocationService(
            registry,
            SQLiteExecutionRepository(self.sessions, clock=now),
            security=self.gate,
        )
        handler = EnterpriseReadHandler(
            self.provider,
            self.authentication,
            self.provider,
            self.provider,
            self.protection,
            self,
            self.gate,
        )
        wakeups = ExecutionWakeupService(SQLiteExecutionControlRepository(self.sessions, clock=now))
        engine = ExecutionEngine(
            invocations,
            {
                CapabilityKind.FUNCTION: FunctionExecutor({READ: handler, DERIVE: self.derive}),
                CapabilityKind.SKILL: SkillExecutor(),
                CapabilityKind.WORKFLOW: WorkflowExecutor(),
                CapabilityKind.AGENT: AgentExecutor(self.no_model),
            },
            clock=now,
            authentication_waits=self.requests,
            wakeups=wakeups,
        )

        async def invoke(version: CapabilityVersion) -> Execution:
            return await invocations.invoke(
                Invocation(
                    capability=CapabilityReference(
                        capability_id=version.capability_id,
                        version=version.number,
                    ),
                    context=InvocationContext(
                        scope=SCOPE, actor_id=SCOPE.user_id, permission_ceiling=(self.permission,)
                    ),
                    budget=ExecutionBudget(max_duration_seconds=60),
                )
            )

        try:
            allowed = await engine.run(SCOPE, (await invoke(versions[READ])).id)
            if allowed.state != ExecutionState.SUCCEEDED or self.frames != 1:
                raise RuntimeFailure("The sample ALLOW scenario failed.")
            self.state.display_allowed = False
            denied = await engine.run(SCOPE, (await invoke(versions[READ])).id)
            if denied.error != "data_use_denied" or self.frames != 1:
                raise RuntimeFailure("The sample DENY scenario failed.")
            self.state.display_allowed = True
            self.broker.ready = False
            pending = await engine.run(SCOPE, (await invoke(versions[READ])).id)
            if pending.wait_reason != "waiting_for_authentication":
                raise RuntimeFailure("The sample authentication did not suspend.")
            request = (await self.requests.list(SCOPE))[0]
            automatic_resumes = await wakeups.run_due()
            self.broker.ready = True
            await engine.resume(SCOPE, pending.id)
            resumed = await engine.run(SCOPE, pending.id)
            completed_request = await self.requests.get(SCOPE, request.id)
            if (
                resumed.state != ExecutionState.SUCCEEDED
                or self.frames != 2
                or automatic_resumes != 0
                or completed_request.state != "satisfied"
                or resumed.deadline_at != pending.deadline_at
            ):
                raise RuntimeFailure("The sample authentication recovery failed.")
            derived = await engine.run(SCOPE, (await invoke(versions[DERIVE])).id)
            metadata = self.derived_metadata
            if (
                derived.state != ExecutionState.SUCCEEDED
                or metadata is None
                or not metadata.protection_context.derived
                or metadata.protection_context.sources != (self.provider.plans[derived.id].source,)
                or metadata.protection_context.rights
                != (ContentRight.DISPLAY, ContentRight.READ, ContentRight.TRANSFORM)
            ):
                raise RuntimeFailure("The derived scenario did not produce protected metadata.")
            async with self.sessions() as session:
                audits = list(await session.scalars(select(ProtectionAudit.event)))
            return {
                "synthetic_only": True,
                "production_ready": False,
                "allow": allowed.state.value,
                "deny": {"state": denied.state.value, "error": denied.error},
                "authentication": {
                    "waiting": pending.wait_reason,
                    "automatic_resumes": automatic_resumes,
                    "resumed": resumed.state.value,
                    "request": completed_request.state,
                    "same_deadline": pending.deadline_at == resumed.deadline_at,
                },
                "derived": {
                    "state": derived.state.value,
                    "receipt": {"status": "derived", "embedding": "denied"},
                    "source_bound": metadata.protection_context.source_bound,
                    "source_count": len(metadata.protection_context.sources),
                    "classification": metadata.protection_context.classification_id,
                    "rights": [right.value for right in metadata.protection_context.rights or ()],
                    "body_recovery": "unavailable",
                },
                "display_frames": self.frames,
                "transport_calls": self.provider.transport_calls,
                "broker_calls": self.broker.calls,
                "audit_decisions": len(audits),
            }
        finally:
            await engine.close()

    @staticmethod
    def no_model() -> Never:
        raise RuntimeFailure("The synthetic sample never creates an Agent or model client.")


async def run_sample(display: Callable[[JsonValue], None]) -> dict[str, JsonValue]:
    with TemporaryDirectory(prefix="shovel-enterprise-sample-") as directory:
        database = create_async_database_engine(f"sqlite+aiosqlite:///{Path(directory) / 'sample.db'}")
        engine = await anext(database)
        try:
            await upgrade_database(engine)
            sessions = create_async_session_factory(engine)
            identities = IdentityService(SQLiteIdentityRepository(sessions, clock=now))
            identity = await identities.create(
                SCOPE, IdentityDraft(display_name="Synthetic only", tenant_binding=TENANT)
            )
            enabled_identity = identity.profile.model_copy(
                update={"enabled": True, "broker_type": BrokerType.PYTHON_WAM}
            )
            # Only these in-memory copies are enabled. Persisted configuration remains disabled/unavailable.
            temporary_profile = EnterpriseConnectionProfile(
                id="synthetic",
                display_name="Synthetic only",
                provider_id="synthetic",
                connection_type="documents",
                environment="sample",
                cloud=Cloud.AZURE_GLOBAL,
                endpoint="https://documents.example.invalid",
                tenant_binding=TENANT,
                resource_scope=("fictional-document",),
                identity_profile_ref=identity.profile.id,
                protection_profile_ref="synthetic-policy",
                enabled=True,
                status="validated",
                created_at=now(),
                updated_at=now(),
            )
            provider = SyntheticProvider(enabled_identity, temporary_profile)
            connections = ConnectionService(
                SQLiteConnectionRepository(sessions, clock=now),
                InstalledProviderCatalog((provider.manifest,), runtime_version="0.1.2"),
            )
            connection = await connections.create(
                SCOPE,
                ConnectionDraft.model_validate(
                    temporary_profile.model_dump(
                        exclude={
                            "format_version",
                            "id",
                            "enabled",
                            "status",
                            "created_at",
                            "updated_at",
                            "revision",
                        }
                    )
                ),
            )
            provider.profile = connection.profile.model_copy(update={"enabled": True, "status": "validated"})
            return await Sample(sessions, provider, display).scenarios()
        finally:
            await database.aclose()


def main() -> None:
    def display(value: JsonValue) -> None:
        print(json.dumps({"authorized_synthetic_display": value}))

    report = asyncio.run(run_sample(display))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
