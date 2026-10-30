from __future__ import annotations

import builtins

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityDefinition,
    CapabilityKind,
    CapabilityScope,
    CapabilityVersion,
)
from shovel.db.base import now_ts
from shovel.db.model.capability import CapabilityDefinition as DefinitionRow
from shovel.db.model.capability import CapabilityDependency as DependencyRow
from shovel.db.model.capability import CapabilityVersion as VersionRow
from shovel.db.model.capability import CapabilityVersionAvailability as AvailabilityRow
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.safe_data import ensure_no_credentials
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.workflow.validation import workflow_errors


def _definition(row: DefinitionRow) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=row.id,
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        kind=CapabilityKind(row.kind),
        name=row.name,
        description=row.description,
        availability=Availability(row.availability),
        created_at=row.created_at,
    )


def _version(row: VersionRow, availability: AvailabilityRow) -> CapabilityVersion:
    return CapabilityVersion(
        id=row.id,
        capability_id=row.capability_id,
        number=row.number,
        revision=row.revision,
        content=CapabilityContent.model_validate(row.content),
        availability=Availability(availability.availability),
        created_at=row.created_at,
        published_at=row.published_at,
        published_by=row.published_by,
        content_hash=row.content_hash,
        based_on_version_id=row.based_on_version_id,
    )


class SQLiteCapabilityRepository:
    async def latest_published(self, scope: CapabilityScope, identifier: str) -> CapabilityVersion:
        async with self.sessions() as session:
            await self._find_definition(session, scope, identifier)
            number = await session.scalar(
                select(VersionRow.number)
                .where(
                    VersionRow.capability_id == identifier,
                    VersionRow.published_at.is_not(None),
                )
                .order_by(VersionRow.number.desc())
                .limit(1)
            )
            if number is None:
                raise RuntimeFailure("No published capability version exists.", status=409)
            row, availability = await self._find_version(session, identifier, number)
            return _version(row, availability)

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def _find_definition(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        identifier: str,
    ) -> DefinitionRow:
        row = await session.scalar(
            select(DefinitionRow).where(
                DefinitionRow.id == identifier,
                DefinitionRow.workspace_id == scope.workspace_id,
                DefinitionRow.user_id == scope.user_id,
            ),
        )
        if row is None:
            raise NotFound("Capability", identifier)
        return row

    async def _find_version(
        self,
        session: AsyncSession,
        identifier: str,
        number: int,
    ) -> tuple[VersionRow, AvailabilityRow]:
        row = await session.scalar(
            select(VersionRow).where(VersionRow.capability_id == identifier, VersionRow.number == number),
        )
        if row is None:
            raise NotFound("Capability version", f"{identifier}:{number}")
        availability = await session.get(AvailabilityRow, row.id)
        if availability is None:
            raise RuntimeFailure("Capability availability record is missing.", status=409)
        return row, availability

    async def _add_version(
        self,
        session: AsyncSession,
        definition: DefinitionRow,
        number: int,
        content: CapabilityContent,
        *,
        based_on: str | None = None,
    ) -> tuple[VersionRow, AvailabilityRow]:
        ensure_no_credentials(content)
        content = CapabilityContent.model_validate(content.model_dump(mode="json"))
        if content.implementation.kind != definition.kind:
            raise RuntimeFailure("Implementation kind does not match the definition.")
        row = VersionRow(
            capability_id=definition.id,
            number=number,
            content=content.model_dump(mode="json"),
            based_on_version_id=based_on,
        )
        session.add(row)
        await session.flush()
        availability = AvailabilityRow(version_id=row.id)
        session.add(availability)
        await session.flush()
        return row, availability

    async def create(
        self,
        scope: CapabilityScope,
        kind: CapabilityKind,
        name: str,
        description: str,
        content: CapabilityContent,
    ) -> CapabilityVersion:
        async with sqlite_write(self.sessions) as session:
            definition = DefinitionRow(
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
                kind=kind.value,
                name=name,
                description=description,
            )
            session.add(definition)
            await session.flush()
            row, availability = await self._add_version(session, definition, 1, content)
            await session.commit()
            return _version(row, availability)

    async def get(self, scope: CapabilityScope, identifier: str) -> CapabilityDefinition:
        async with self.sessions() as session:
            return _definition(await self._find_definition(session, scope, identifier))

    async def get_version(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
    ) -> CapabilityVersion:
        async with self.sessions() as session:
            await self._find_definition(session, scope, identifier)
            return _version(*(await self._find_version(session, identifier, number)))

    async def list(
        self,
        scope: CapabilityScope,
        *,
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityDefinition]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise RuntimeFailure("Invalid capability pagination.")
        statement = select(DefinitionRow).where(
            DefinitionRow.workspace_id == scope.workspace_id,
            DefinitionRow.user_id == scope.user_id,
        )
        if query:
            statement = statement.where(
                or_(
                    DefinitionRow.name.icontains(query, autoescape=True),
                    DefinitionRow.description.icontains(query, autoescape=True),
                ),
            )
        async with self.sessions() as session:
            rows = await session.scalars(
                statement.order_by(DefinitionRow.created_at, DefinitionRow.id).limit(limit).offset(offset),
            )
            return [_definition(row) for row in rows]

    async def update_draft(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
        content: CapabilityContent,
        *,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
    ) -> CapabilityVersion:
        async with sqlite_write(self.sessions) as session:
            definition = await self._find_definition(session, scope, identifier)
            row, availability = await self._find_version(session, identifier, number)
            self._editable(definition, row, availability, expected_revision)
            ensure_no_credentials(content)
            content = CapabilityContent.model_validate(content.model_dump(mode="json"))
            if content.implementation.kind != definition.kind:
                raise RuntimeFailure("Implementation kind does not match the definition.")
            if name is not None:
                definition.name = name
            if description is not None:
                definition.description = description
            row.content = content.model_dump(mode="json")
            row.revision += 1
            await session.commit()
            return _version(row, availability)

    @staticmethod
    def _editable(
        definition: DefinitionRow,
        row: VersionRow,
        availability: AvailabilityRow,
        expected_revision: int,
    ) -> None:
        if row.published_at is not None:
            raise RuntimeFailure("Published versions are immutable; create a new draft.", status=409)
        if definition.availability != "active" or availability.availability != "active":
            raise RuntimeFailure("Capability draft is unavailable.", status=409)
        if row.revision != expected_revision:
            raise RuntimeFailure("Capability draft has changed; reload before saving.", status=409)

    async def fork(self, scope: CapabilityScope, identifier: str, number: int) -> CapabilityVersion:
        async with sqlite_write(self.sessions) as session:
            definition = await self._find_definition(session, scope, identifier)
            source, _ = await self._find_version(session, identifier, number)
            if source.published_at is None:
                raise RuntimeFailure("Only published versions can be copied into a new draft.", status=409)
            if definition.availability in {"disabled", "archived"}:
                raise RuntimeFailure("Capability is unavailable.", status=409)
            maximum = await session.scalar(
                select(func.max(VersionRow.number)).where(VersionRow.capability_id == identifier),
            )
            row, availability = await self._add_version(
                session,
                definition,
                int(maximum or 0) + 1,
                CapabilityContent.model_validate(source.content),
                based_on=source.id,
            )
            await session.commit()
            return _version(row, availability)

    async def _dependency_targets(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        content: CapabilityContent,
    ) -> tuple[builtins.list[VersionRow], tuple[str, ...]]:
        targets: list[VersionRow] = []
        parsed_targets: dict[tuple[str, int], CapabilityContent] = {}
        errors: list[str] = []
        locked = {
            (item.capability_id, item.version): item.content_hash
            for item in content.dependency_lock.dependencies
        }
        for dependency in content.dependency_lock.dependencies:
            result = (
                await session.execute(
                    select(VersionRow, AvailabilityRow, DefinitionRow)
                    .join(DefinitionRow, VersionRow.capability_id == DefinitionRow.id)
                    .join(AvailabilityRow, AvailabilityRow.version_id == VersionRow.id)
                    .where(
                        DefinitionRow.workspace_id == scope.workspace_id,
                        DefinitionRow.user_id == scope.user_id,
                        VersionRow.capability_id == dependency.capability_id,
                        VersionRow.number == dependency.version,
                    ),
                )
            ).one_or_none()
            if result is None:
                errors.append("Dependency exact version is missing or belongs to another owner.")
                continue
            row, availability, definition = result
            if row.published_at is None:
                errors.append("Dependency must be published.")
            if {availability.availability, definition.availability} & {"disabled", "archived"}:
                errors.append("Dependency is unavailable.")
            parsed = CapabilityContent.model_validate(row.content)
            parsed_targets[(row.capability_id, row.number)] = parsed
            if row.content_hash != dependency.content_hash or parsed.content_hash != dependency.content_hash:
                errors.append("Dependency content hash does not match.")
            if any(
                locked.get((item.capability_id, item.version)) != item.content_hash
                for item in parsed.dependency_lock.dependencies
            ):
                errors.append("Dependency lock must include the complete transitive lock.")
            targets.append(row)
        errors.extend(workflow_errors(content, parsed_targets))
        return targets, tuple(errors)

    async def dependency_errors(
        self,
        scope: CapabilityScope,
        content: CapabilityContent,
    ) -> tuple[str, ...]:
        async with self.sessions() as session:
            _, errors = await self._dependency_targets(session, scope, content)
            return errors

    async def publish(
        self,
        scope: CapabilityScope,
        identifier: str,
        number: int,
        *,
        expected_revision: int,
        content_hash: str,
    ) -> CapabilityVersion:
        async with sqlite_write(self.sessions) as session:
            definition = await self._find_definition(session, scope, identifier)
            row, availability = await self._find_version(session, identifier, number)
            self._editable(definition, row, availability, expected_revision)
            content = CapabilityContent.model_validate(row.content)
            if content.content_hash != content_hash:
                raise RuntimeFailure("Capability changed after publish authorization.", status=409)
            targets, errors = await self._dependency_targets(session, scope, content)
            if any(target.capability_id == identifier for target in targets):
                errors = (*errors, "Capability cycles are not allowed.")
            if errors:
                raise RuntimeFailure("Invalid dependency lock: " + "; ".join(errors), status=409)
            await session.execute(delete(DependencyRow).where(DependencyRow.owner_version_id == row.id))
            session.add_all(
                DependencyRow(owner_version_id=row.id, target_version_id=target.id) for target in targets
            )
            # Lock rows must be flushed while the owning version is still a draft.
            await session.flush()
            row.content_hash = content_hash
            row.published_at = now_ts()
            row.published_by = scope.user_id
            row.revision += 1
            await session.commit()
            return _version(row, availability)

    async def set_availability(
        self,
        scope: CapabilityScope,
        identifier: str,
        availability: Availability,
        *,
        number: int | None = None,
    ) -> None:
        async with sqlite_write(self.sessions) as session:
            definition = await self._find_definition(session, scope, identifier)
            if number is None:
                definition.availability = availability.value
            else:
                row, state = await self._find_version(session, identifier, number)
                if row.published_at is None and availability == Availability.DEPRECATED:
                    raise RuntimeFailure("An unpublished draft cannot be deprecated.", status=409)
                state.availability = availability.value
            await session.commit()
