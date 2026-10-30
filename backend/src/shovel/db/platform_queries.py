from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityDefinition, CapabilityKind, CapabilityScope, CapabilityVersion
from shovel.db.approval_repository import _approval
from shovel.db.capability_repository import SQLiteCapabilityRepository, _definition
from shovel.db.execution_repository import _record
from shovel.db.model.capability import CapabilityDefinition as DefinitionRow
from shovel.db.model.capability import CapabilityVersion as VersionRow
from shovel.db.model.runtime import Operation
from shovel.db.model.security import ApprovalRequest as ApprovalRow
from shovel.execution.domain import Execution
from shovel.platform.domain import ExecutionTiming
from shovel.security.domain import ApprovalRequest


class SQLitePlatformQueries:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.capabilities_repository = SQLiteCapabilityRepository(sessions)

    async def capabilities(
        self, scope: CapabilityScope, kind: CapabilityKind | None, query: str
    ) -> list[CapabilityDefinition]:
        statement = select(DefinitionRow).where(
            DefinitionRow.workspace_id == scope.workspace_id,
            DefinitionRow.user_id == scope.user_id,
        )
        if kind is not None:
            statement = statement.where(DefinitionRow.kind == kind.value)
        if query:
            statement = statement.where(
                or_(
                    DefinitionRow.name.contains(query, autoescape=True),
                    DefinitionRow.description.contains(query, autoescape=True),
                )
            )
        async with self.sessions() as session:
            return [
                _definition(row)
                for row in await session.scalars(statement.order_by(DefinitionRow.id).limit(100))
            ]

    async def versions(self, scope: CapabilityScope, identifier: str) -> list[CapabilityVersion]:
        await self.capabilities_repository.get(scope, identifier)
        async with self.sessions() as session:
            numbers = list(
                await session.scalars(
                    select(VersionRow.number)
                    .where(
                        VersionRow.capability_id == identifier,
                    )
                    .order_by(VersionRow.number.desc())
                    .limit(100)
                )
            )
        return [
            await self.capabilities_repository.get_version(scope, identifier, number) for number in numbers
        ]

    async def executions(self, scope: CapabilityScope, service_id: str | None = None) -> list[Execution]:
        statement = select(Operation).where(
            Operation.execution_data.is_not(None),
            func.json_extract(Operation.execution_data, "$.context.scope.workspace_id") == scope.workspace_id,
            func.json_extract(Operation.execution_data, "$.context.scope.user_id") == scope.user_id,
        )
        if service_id is not None:
            statement = statement.where(
                func.json_extract(Operation.execution_data, "$.context.proactive_snapshot.id") == service_id
            )
        async with self.sessions() as session:
            return [
                _record(row)
                for row in await session.scalars(
                    statement.order_by(Operation.queued_at.desc(), Operation.id.desc()).limit(100)
                )
            ]

    async def approvals(self, scope: CapabilityScope) -> list[ApprovalRequest]:
        async with self.sessions() as session:
            return [
                _approval(row)
                for row in await session.scalars(
                    select(ApprovalRow)
                    .where(
                        ApprovalRow.workspace_id == scope.workspace_id,
                        ApprovalRow.user_id == scope.user_id,
                    )
                    .order_by(ApprovalRow.created_at.desc(), ApprovalRow.id)
                    .limit(100)
                )
            ]

    async def timings(self, scope: CapabilityScope, identifiers: list[str]) -> dict[str, ExecutionTiming]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(Operation.id, Operation.started_at, Operation.finished_at).where(
                    Operation.id.in_(identifiers),
                    Operation.execution_data.is_not(None),
                    func.json_extract(Operation.execution_data, "$.context.scope.workspace_id")
                    == scope.workspace_id,
                    func.json_extract(Operation.execution_data, "$.context.scope.user_id") == scope.user_id,
                )
            )
            return {
                row.id: ExecutionTiming(started_at=row.started_at, finished_at=row.finished_at)
                for row in rows
            }
