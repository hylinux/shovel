from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.model.organization import OrganizationRevision, OrganizationSource
from shovel.db.protection_repository import SQLiteProtectionRepository
from shovel.db.unit_of_work import sqlite_write
from shovel.enterprise.domain import EnterpriseResourceRef
from shovel.enterprise.kusto import KustoObservation, metadata_resource
from shovel.enterprise.organization import MetadataConnection, VerifiedOrganizationPolicy
from shovel.protection.domain import ProtectionDeniedError, content_digest
from shovel.protection.persistence import SourceChange


class SQLiteOrganizationRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], protection: SQLiteProtectionRepository
    ) -> None:
        self.sessions, self.protection = sessions, protection

    async def _block(self, session: AsyncSession, row: OrganizationSource, reason: str, digest: str) -> None:
        change = SourceChange.model_validate(
            {
                "event_id": content_digest(
                    {
                        "connection": row.connection_id,
                        "generation": row.generation,
                        "reason": reason,
                        "digest": digest,
                    }
                ),
                "scope": {"workspace_id": row.workspace_id, "user_id": row.user_id},
                "resource": row.resource,
                "expected_generation": row.generation,
                "reason": reason,
            }
        )
        row.generation = await self.protection._invalidate(session, change)
        row.blocked = True

    async def accept(self, verified: VerifiedOrganizationPolicy) -> None:
        policy = verified.policy
        scope = policy.scope
        async with sqlite_write(self.sessions) as session:
            key = scope.workspace_id, scope.user_id, policy.organization_id
            current = await session.get(OrganizationRevision, key)
            if current is not None:
                if current.revision > policy.revision or (
                    current.revision == policy.revision and current.policy_digest != verified.digest
                ):
                    raise ProtectionDeniedError("Organization policy rollback or equivocation rejected.")
                if current.policy_digest == verified.digest:
                    return
                current.revision, current.policy_digest = policy.revision, verified.digest
            else:
                session.add(
                    OrganizationRevision(
                        workspace_id=scope.workspace_id,
                        user_id=scope.user_id,
                        organization_id=policy.organization_id,
                        revision=policy.revision,
                        policy_digest=verified.digest,
                    )
                )
            rows = await session.scalars(
                select(OrganizationSource).where(
                    OrganizationSource.workspace_id == scope.workspace_id,
                    OrganizationSource.user_id == scope.user_id,
                    OrganizationSource.blocked.is_(False),
                )
            )
            for row in rows:
                await self._block(session, row, "classification_changed", verified.digest)
            await session.commit()

    async def observe(
        self,
        verified: VerifiedOrganizationPolicy,
        connection: MetadataConnection,
        observation: KustoObservation,
    ) -> EnterpriseResourceRef:
        policy, scope = verified.policy, verified.policy.scope
        resource = metadata_resource(connection, observation.version)
        denied = False
        async with sqlite_write(self.sessions) as session:
            revision = await session.get(
                OrganizationRevision, (scope.workspace_id, scope.user_id, policy.organization_id)
            )
            if revision is None or revision.policy_digest != verified.digest:
                raise ProtectionDeniedError("Organization policy changed before source observation.")
            key = scope.workspace_id, scope.user_id, policy.organization_id, connection.profile.id
            row = await session.get(OrganizationSource, key)
            if row is None:
                row = OrganizationSource(
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    organization_id=policy.organization_id,
                    connection_id=connection.profile.id,
                    resource=resource.model_dump(mode="json"),
                    policy_digest=verified.digest,
                    generation=0,
                    blocked=False,
                    observed_at=observation.observed_at,
                )
                session.add(row)
            elif observation.observed_at < row.observed_at:
                raise ProtectionDeniedError("Late source observation rejected.")
            if not observation.present:
                await self._block(session, row, "source_deleted", observation.version)
            elif not row.blocked and row.resource != resource.model_dump(mode="json"):
                await self._block(session, row, "source_changed", observation.version)
            row.observed_at = observation.observed_at
            denied = row.blocked
            await session.commit()
        if denied:
            raise ProtectionDeniedError(
                "Source binding changed or is tombstoned; explicit reauthorization required."
            )
        return resource

    async def revoke(self, verified: VerifiedOrganizationPolicy, connection: MetadataConnection) -> None:
        scope = verified.policy.scope
        async with sqlite_write(self.sessions) as session:
            row = await session.get(
                OrganizationSource,
                (scope.workspace_id, scope.user_id, verified.policy.organization_id, connection.profile.id),
            )
            if row is None:
                row = OrganizationSource(
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    organization_id=verified.policy.organization_id,
                    connection_id=connection.profile.id,
                    resource=metadata_resource(connection, "0" * 64).model_dump(mode="json"),
                    policy_digest=verified.digest,
                    generation=0,
                    blocked=False,
                    observed_at=self.protection._clock(),
                )
                session.add(row)
            if not row.blocked:
                await self._block(session, row, "source_revoked", verified.digest)
            await session.commit()

    async def revoke_all(self, verified: VerifiedOrganizationPolicy) -> None:
        scope = verified.policy.scope
        async with sqlite_write(self.sessions) as session:
            rows = await session.scalars(
                select(OrganizationSource).where(
                    OrganizationSource.workspace_id == scope.workspace_id,
                    OrganizationSource.user_id == scope.user_id,
                    OrganizationSource.organization_id == verified.policy.organization_id,
                    OrganizationSource.blocked.is_(False),
                )
            )
            for row in rows:
                await self._block(session, row, "source_revoked", verified.digest)
            await session.commit()
