from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import structlog
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityReference, FunctionImplementation, Permission
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.organization_repository import SQLiteOrganizationRepository
from shovel.db.protection_audit import SQLitePolicyAudit
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import ConnectionBinding, EnterpriseResourceRef, ProviderManifest
from shovel.enterprise.execution import EnterpriseReadPlan
from shovel.enterprise.kusto import KustoMetadataClient, KustoObservation, KustoSourceDenied
from shovel.enterprise.organization import (
    MetadataConnection,
    OrganizationLoader,
    VerifiedOrganizationPolicy,
    installed_organization_loader,
    verify_policy,
)
from shovel.enterprise.rollout import RolloutGate, installed_rollout
from shovel.enterprise.transport import ProviderRequest
from shovel.execution.domain import (
    CapabilitySnapshot,
    Execution,
    ExecutionBudget,
    ExecutionSnapshot,
    InvocationContext,
)
from shovel.execution.engine import ExecutionEngine
from shovel.identity.domain import AuthContext, TokenRequest
from shovel.identity.service import AuthenticationService
from shovel.identity.waits import authentication_execution_lease
from shovel.protection.domain import (
    AuthorizationSnapshot,
    ContentRight,
    DataAction,
    DataDestination,
    DataUseBinding,
    DependencyKind,
    Lineage,
    LineageEdge,
    PolicyOutcome,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionContext,
    ProtectionDeniedError,
    SourceBinding,
    SourceContentRef,
    content_digest,
)
from shovel.protection.governance import GovernanceService
from shovel.protection.policy import (
    DataRule,
    PolicyState,
    ProtectionPolicy,
    SourceAuthorization,
)
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import SecurityDenied


@dataclass(frozen=True)
class MetadataEvidence:
    connection: MetadataConnection
    observation: KustoObservation
    resource: EnterpriseResourceRef
    deadline: float


class OrganizationCatalog(InstalledProviderCatalog):
    def __init__(self, service: OrganizationService, runtime_version: str) -> None:
        super().__init__(runtime_version=runtime_version)
        self.service, self.runtime_version = service, runtime_version

    def _catalog(self) -> InstalledProviderCatalog:
        try:
            current = self.service.current(optional=True)
        except ProtectionDeniedError:
            raise RuntimeFailure("Organization catalog trust is unavailable.", status=409) from None
        manifests = (
            ()
            if current is None or current.policy.revoked or not current.policy.connections
            else (current.policy.manifest,)
        )
        return InstalledProviderCatalog(manifests, runtime_version=self.runtime_version)

    def get(self, provider_id: str) -> ProviderManifest:
        return self._catalog().get(provider_id)

    def list(self) -> tuple[ProviderManifest, ...]:
        return self._catalog().list()


class OrganizationService:
    """Trusted metadata-only resolver, policy provider and source observer in the existing Host."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authentication: AuthenticationService,
        engine: ExecutionEngine,
        *,
        runtime_version: str,
        loader: OrganizationLoader | None = None,
        client: KustoMetadataClient | None = None,
        clock: Callable[[], float] = time.time,
        governance: GovernanceService | None = None,
        rollout: RolloutGate | None = None,
    ) -> None:
        self.loader = loader if loader is not None else installed_organization_loader()
        self.authentication, self.engine, self.clock = authentication, engine, clock
        self.client = client or KustoMetadataClient(clock=clock)
        self.rollout = rollout if rollout is not None else installed_rollout()
        self.connections, self.identities = (
            SQLiteConnectionRepository(sessions),
            SQLiteIdentityRepository(sessions),
        )
        self.policy = ProtectionPolicy(
            SQLitePolicyAudit(), provider=OrganizationPolicyProvider(self), clock=clock
        )
        self.protection = SQLiteProtectionRepository(
            sessions, self.policy, clock=clock, governance=governance, release_guard=self.release_guard
        )
        self.journal = SQLiteOrganizationRepository(sessions, self.protection)
        self.catalog = OrganizationCatalog(self, runtime_version)
        self._accepted: VerifiedOrganizationPolicy | None = None
        self._evidence: dict[str, MetadataEvidence] = {}
        self._refresh_lock = asyncio.Lock()
        self._last_failure = False

    def _load(self) -> VerifiedOrganizationPolicy | None:
        if self.loader is None:
            return None
        try:
            supplied = self.loader()
            if supplied is None:
                return None
            current = verify_policy(supplied[1], supplied[0], now=self.clock())
            self.protection.governance.require(
                scope=current.policy.scope,
                organization_id=current.policy.organization_id,
                store_binding=self.protection.store_binding,
                kind="control_lineage",
                record_digest=current.digest,
                created_at=self.clock(),
                operation="persist",
            )
            return current
        except ProtectionDeniedError:
            raise
        except Exception:
            raise ProtectionDeniedError("Organization machine trust is unavailable.") from None

    def current(self, *, optional: bool = False) -> VerifiedOrganizationPolicy | None:
        current = self._load()
        if current is None and self._accepted is None and optional:
            return None
        if current is None or self._accepted is None or current != self._accepted:
            raise ProtectionDeniedError("Organization trust changed or has not been admitted.")
        return current

    async def refresh_policy(self) -> None:
        if self.loader is None:
            return
        async with self._refresh_lock:
            try:
                current = await asyncio.to_thread(self._load)
            except ProtectionDeniedError:
                previous = self._accepted
                self._accepted = None
                self._evidence.clear()
                if previous is not None:
                    await self.journal.revoke_all(previous)
                if not self._last_failure:
                    structlog.get_logger("shovel.enterprise").warning("organization.trust_unavailable")
                self._last_failure = True
                return
            self._last_failure = False
            if current == self._accepted:
                if current is not None:
                    await self.protection.purge_retention()
                    self._evidence = {
                        key: value
                        for key, value in self._evidence.items()
                        if value.observation.observed_at + current.policy.metadata_max_age_seconds
                        > self.clock()
                    }
                return
            previous = self._accepted
            self._accepted = None
            self._evidence.clear()
            if current is not None:
                if not current.policy.revoked and current.policy.connections:
                    InstalledProviderCatalog(
                        (current.policy.manifest,), runtime_version=self.catalog.runtime_version
                    )
                await self.journal.accept(current)
            elif previous is not None:
                await self.journal.revoke_all(previous)
            self._accepted = current
            if current is not None:
                await self.protection.purge_retention()

    def _trusted(self) -> VerifiedOrganizationPolicy:
        current = self.current()
        if current is None or current.policy.revoked:
            raise ProtectionDeniedError("Organization policy is unavailable or revoked.")
        return current

    @staticmethod
    def connection_digest(current: VerifiedOrganizationPolicy, connection: MetadataConnection) -> str:
        return content_digest(
            {
                "connection": connection.model_dump(mode="json"),
                "manifest": current.policy.manifest.content_hash,
            }
        )

    def _rollout(
        self,
        snapshot: ExecutionSnapshot,
        *,
        created_at: float,
        action: DataAction = DataAction.READ,
        destination: DataDestination = DataDestination.WORKFLOW_STATE,
        expected: str | None = None,
    ) -> str | None:
        permissions = [item for item in snapshot.granted_permissions if item.action.startswith("enterprise.")]
        requirements = snapshot.capability.content.connection_requirements
        if not permissions and not requirements:
            return None
        current = self._trusted()
        if (
            len(permissions) != 1
            or permissions[0].action != "enterprise.read"
            or snapshot.context.scope != current.policy.scope
        ):
            raise ProtectionDeniedError("Rollout requires one exact scoped enterprise READ permission.")
        matches = [item for item in current.policy.connections if item.profile.id == permissions[0].resource]
        if len(matches) != 1 or any(item.connection_ref != permissions[0].resource for item in requirements):
            raise ProtectionDeniedError("Rollout connection does not match the frozen execution.")
        connection = matches[0]
        current.policy.validate_connection(connection)
        _, governance_digest = self.protection.governance.policy()
        return self.rollout.require(
            scope=snapshot.context.scope,
            organization_id=current.policy.organization_id,
            connection_id=connection.profile.id,
            organization_policy_digest=current.digest,
            governance_policy_digest=governance_digest,
            provider_manifest_digest=current.policy.manifest.content_hash,
            connection_binding_digest=self.connection_digest(current, connection),
            execution_created_at=created_at,
            action=action,
            destination=destination,
            expected=expected,
        )

    def admit(self, snapshot: ExecutionSnapshot) -> str | None:
        return self._rollout(snapshot, created_at=self.clock())

    def authorize_connection(
        self,
        capability: CapabilitySnapshot,
        context: InvocationContext,
        permissions: tuple[Permission, ...],
    ) -> None:
        content = capability.content
        if (
            not isinstance(content.implementation, FunctionImplementation)
            or content.implementation.implementation_id != "kusto.metadata.read"
            or content.side_effect_profile.kind != "none"
            or len(content.connection_requirements) != 1
        ):
            raise SecurityDenied("Rollout permits only the installed metadata-read Function.")
        try:
            self.admit(
                ExecutionSnapshot(
                    capability=capability,
                    context=context,
                    budget=ExecutionBudget(),
                    capability_call_path=(
                        CapabilityReference(
                            capability_id=capability.capability_id, version=capability.number
                        ),
                    ),
                    granted_permissions=permissions,
                )
            )
        except ProtectionDeniedError:
            raise SecurityDenied("Current enterprise rollout does not authorize this connection.") from None

    def revalidate_execution(self, execution: Execution) -> None:
        snapshot = execution.snapshot
        if (
            snapshot.enterprise_rollout_binding is None
            and not snapshot.capability.content.connection_requirements
            and not any(item.action.startswith("enterprise.") for item in snapshot.granted_permissions)
        ):
            return
        try:
            self._require_rollout(execution)
        except ProtectionDeniedError:
            raise SecurityDenied("Frozen enterprise rollout admission is no longer current.") from None

    def _require_rollout(
        self,
        execution: Execution,
        *,
        action: DataAction = DataAction.READ,
        destination: DataDestination = DataDestination.WORKFLOW_STATE,
    ) -> None:
        expected = execution.snapshot.enterprise_rollout_binding
        if (
            expected is None
            or self._rollout(
                execution.snapshot,
                created_at=execution.created_at,
                action=action,
                destination=destination,
                expected=expected,
            )
            != expected
        ):
            raise ProtectionDeniedError("Execution has no current frozen enterprise rollout admission.")

    @contextmanager
    def release_guard(self, binding: DataUseBinding) -> Iterator[None]:
        lease = authentication_execution_lease.get()
        if lease is None or lease.execution.id != binding.execution_id:
            raise ProtectionDeniedError("Rollout release requires the current owned Execution.")
        with self.rollout.release():
            self._require_rollout(lease.execution, action=binding.action, destination=binding.destination)
            yield

    async def _configuration(
        self, execution: Execution
    ) -> tuple[VerifiedOrganizationPolicy, MetadataConnection]:
        self._require_rollout(execution)
        current = self._trusted()
        policy = current.policy
        lease = authentication_execution_lease.get()
        requirements = execution.snapshot.capability.content.connection_requirements
        if (
            lease is None
            or lease.execution.id != execution.id
            or execution.snapshot.context.scope != policy.scope
            or len(requirements) != 1
        ):
            raise ProtectionDeniedError("Organization metadata requires its owned, scoped Execution.")
        matches = [item for item in policy.connections if item.profile.id == requirements[0].connection_ref]
        if len(matches) != 1:
            raise ProtectionDeniedError("Organization connection is not in the signed catalog.")
        item = matches[0]
        policy.validate_connection(item)
        label = next(label for label in policy.labels if label.id == item.label_id)
        if ContentRight.READ not in label.rights:
            raise ProtectionDeniedError("Organization metadata read right is missing.")
        permission = Permission(action="enterprise.read", resource=item.profile.id)
        content = execution.snapshot.capability.content
        if (
            permission not in execution.snapshot.granted_permissions
            or permission not in content.required_permissions
            or not isinstance(content.implementation, FunctionImplementation)
            or content.implementation.implementation_id != "kusto.metadata.read"
            or content.side_effect_profile.kind != "none"
        ):
            raise ProtectionDeniedError("Execution lacks the organization read grant.")
        await self.engine.repository.check_active(lease, self.engine.invocations.security)
        record = await self.connections.get(policy.scope, item.profile.id)
        identity = await self.identities.get(policy.scope, item.identity.id)
        if (
            record.profile.status == "disabled"
            or record.profile.model_copy(
                update={"enabled": item.profile.enabled, "status": item.profile.status}
            )
            != item.profile
            or record.manifest_digest != policy.manifest.content_hash
            or identity.profile.model_copy(
                update={
                    "enabled": item.identity.enabled,
                    "broker_type": item.identity.broker_type,
                    "status": item.identity.status,
                }
            )
            != item.identity
        ):
            raise ProtectionDeniedError("Connection or identity revision was edited or disabled.")
        return current, item

    async def resolve(self, execution: Execution) -> EnterpriseReadPlan:
        current, connection = await self._configuration(execution)
        request = connection.authentication.model_copy(
            update={
                "request_id": uuid4().hex,
                "execution_id": execution.id,
                "correlation_id": execution.correlation_id,
            }
        )
        self._evidence = {
            key: value
            for key, value in self._evidence.items()
            if value.connection.profile.id != connection.profile.id
            and value.observation.observed_at + current.policy.metadata_max_age_seconds > self.clock()
        }
        credential = await self.authentication.authenticate(request)
        observation = await self._observe(current, connection, request, credential)
        latest, _ = await self._configuration(execution)
        if (
            latest != current
            or observation.observed_at + current.policy.metadata_max_age_seconds <= self.clock()
        ):
            raise ProtectionDeniedError("Organization/source changed during metadata acquisition.")
        resource = await self.journal.observe(current, connection, observation)
        evidence = MetadataEvidence(
            connection,
            observation,
            resource,
            min(execution.deadline_at or 0, current.policy.expires_at),
        )
        self._evidence[execution.id] = evidence
        plan = EnterpriseReadPlan(
            request=ProviderRequest(
                operation_id="kusto.metadata.read",
                connection=ConnectionBinding(
                    connection_profile_id=connection.profile.id,
                    connection_revision=connection.profile.revision,
                    provider_id=current.policy.manifest.provider_id,
                    provider_version=current.policy.manifest.provider_version,
                    manifest_digest=current.policy.manifest.content_hash,
                    identity_profile_id=connection.identity.id,
                    tenant_id=current.policy.tenant_id,
                    endpoint=connection.profile.endpoint,
                ),
                authentication=request,
                resource=resource,
            ),
            profile=connection.profile,
            manifest=current.policy.manifest,
            identity=connection.identity,
            source=SourceBinding(
                resource=resource,
                identity_profile_id=connection.identity.id,
                principal_ref=request.identity.principal_ref,
                source_generation=0,
            ),
        )
        plan.validate_execution(execution)
        await self.protection.check_sources(current.policy.scope, (plan.source,))
        return plan

    async def _observe(
        self,
        current: VerifiedOrganizationPolicy,
        connection: MetadataConnection,
        request: TokenRequest,
        credential: AuthContext,
    ) -> KustoObservation:
        lease = authentication_execution_lease.get()
        if lease is None or lease.execution.id != request.execution_id:
            credential.close()
            raise ProtectionDeniedError("Metadata observation requires its owned Execution.")
        self._evidence = {
            key: value
            for key, value in self._evidence.items()
            if value.connection.profile.id != connection.profile.id
        }
        try:
            self._require_rollout(lease.execution)
            observation = await self.client.observe(connection, request, credential)
        except KustoSourceDenied as error:
            if error.status == 403:
                await self.journal.revoke(current, connection)
            raise
        finally:
            credential.close()
        self._require_rollout(lease.execution)
        if (
            self._trusted() != current
            or observation.observed_at + current.policy.metadata_max_age_seconds <= self.clock()
        ):
            raise ProtectionDeniedError("Organization/source freshness changed during metadata acquisition.")
        return observation

    def protection_context(
        self, evidence: MetadataEvidence, *, revalidate_at: float | None = None
    ) -> ProtectionContext:
        current = self._trusted()
        item = evidence.connection
        current.policy.validate_connection(item)
        label = next(label for label in current.policy.labels if label.id == item.label_id)
        source = SourceBinding(
            resource=evidence.resource,
            identity_profile_id=item.identity.id,
            principal_ref=item.authentication.identity.principal_ref,
            source_generation=0,
        )
        expires = min(
            evidence.observation.observed_at + current.policy.metadata_max_age_seconds,
            current.policy.expires_at,
            evidence.deadline,
        )
        if revalidate_at is not None:
            if revalidate_at > expires:
                raise ProtectionDeniedError("Protection evidence exceeds current source freshness.")
            expires = revalidate_at
        return ProtectionContext(
            id=content_digest({"source": source.model_dump(mode="json"), "policy": current.digest}),
            sources=(source,),
            classification_id=label.classification_id,
            classification_policy_ref=current.policy.organization_id,
            classification_rank=label.rank,
            sensitivity_label_id=label.id,
            protection_status=label.protection_status,
            encrypted=label.encrypted,
            rights=label.rights,
            policy_source_ref=current.policy.organization_id,
            policy_version=current.digest,
            dlp_applicable=label.dlp_applicable,
            restrictions=label.restrictions,
            retention_policy_ref=label.retention_policy_ref,
            expires_at=current.policy.expires_at,
            revalidate_at=expires,
        )

    def resolve_policy(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyState:
        current = self._trusted()
        lease = authentication_execution_lease.get()
        evidence = self._evidence.get(binding.execution_id)
        if (
            evidence is None
            or lease is None
            or lease.execution.id != binding.execution_id
            or context.revalidate_at is None
            or context != self.protection_context(evidence, revalidate_at=context.revalidate_at)
            or binding.identities != (evidence.connection.authentication.identity,)
        ):
            raise ProtectionDeniedError("Organization policy lacks exact live metadata evidence.")
        self._require_rollout(lease.execution, action=binding.action, destination=binding.destination)
        context.require_known(self.clock())
        identity = evidence.connection.authentication.identity
        label = next(label for label in current.policy.labels if label.id == evidence.connection.label_id)
        rules = tuple(
            DataRule(
                id=f"organization.metadata.{action.value}",
                classifications=(label.classification_id,),
                action=action,
                destination=destination,
                outcome=PolicyOutcome.ALLOW,
                permitted_restrictions=label.restrictions,
                retention_policy_refs=(label.retention_policy_ref,),
            )
            for action, destination in (
                (DataAction.READ, DataDestination.WORKFLOW_STATE),
                (DataAction.DISPLAY, DataDestination.USER_SCREEN),
            )
            if action.value in label.rights
        )
        snapshot = AuthorizationSnapshot(
            id=content_digest(
                {"version": evidence.resource.version_key, "at": evidence.observation.observed_at}
            ),
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            principal_ref=identity.principal_ref,
            identity_profile_id=identity.identity_profile_id,
            identity_revision=identity.identity_revision,
            tenant_id=identity.tenant_id,
            resource_scope=(evidence.resource,),
            decision="allow",
            captured_at=evidence.observation.observed_at,
            policy_version=current.digest,
            freshness="fresh",
            revalidate_at=context.revalidate_at,
        )
        return PolicyState(
            context=context,
            binding=binding,
            classifications=(label.classification_id,),
            sources=(
                SourceAuthorization(
                    source=context.sources[0],
                    snapshot=snapshot,
                    active=True,
                    authentication_valid=True,
                ),
            ),
            rules=rules,
            captured_at=snapshot.captured_at,
            revalidate_at=snapshot.revalidate_at,
            execution_deadline=evidence.deadline,
        )

    async def send(
        self,
        request: ProviderRequest,
        authentication: AuthContext,
        *,
        decode: Callable[[bytes], ProtectedContent[JsonValue]],
    ) -> ProtectedContent[JsonValue]:
        evidence = self._evidence.get(request.authentication.execution_id)
        if evidence is None or request.resource != evidence.resource:
            raise ProtectionDeniedError("Kusto transport lacks an exact metadata plan.")
        lease = authentication_execution_lease.get()
        if lease is None or lease.execution.id != request.authentication.execution_id:
            raise ProtectionDeniedError("Kusto transport lacks the current Execution claim.")
        current, connection = await self._configuration(lease.execution)
        if (
            connection != evidence.connection
            or request.operation_id != "kusto.metadata.read"
            or request.parameter_refs
        ):
            raise ProtectionDeniedError("Kusto transport differs from the signed metadata operation.")
        observation = await self._observe(current, connection, request.authentication, authentication)
        resource = await self.journal.observe(current, connection, observation)
        if observation.version != request.resource.source_version:
            raise ProtectionDeniedError("Kusto metadata changed before decoding.")
        self._evidence[request.authentication.execution_id] = MetadataEvidence(
            connection, observation, resource, evidence.deadline
        )
        return decode(observation.body())

    def decode(self, plan: EnterpriseReadPlan, response: bytes) -> ProtectedContent[JsonValue]:
        evidence = self._evidence.get(plan.request.authentication.execution_id)
        if evidence is None or response != evidence.observation.body():
            raise ProtectionDeniedError("Kusto metadata decoder rejected a different response.")
        context = self.protection_context(evidence)
        metadata = ProtectedContentMetadata(
            reference=ProtectedContentRef(
                reference=SourceContentRef(resource=evidence.resource),
                protection_context_ref=context.id,
                content_digest=content_digest(
                    evidence.observation.model_dump(mode="json", exclude={"observed_at"})
                ),
                sources=context.sources,
            ),
            protection_context=context,
            provenance=Lineage(edges=(LineageEdge(source=plan.source, kind=DependencyKind.DATA),)),
            authorization_snapshot_refs=(context.id,),
            created_at=self.clock(),
            lifecycle_state="active",
        )
        return ProtectedContent(
            evidence.observation.model_dump(mode="json", exclude={"observed_at"}), metadata
        )


class OrganizationPolicyProvider:
    def __init__(self, service: OrganizationService) -> None:
        self.service = service

    def resolve(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyState:
        return self.service.resolve_policy(context, binding)
