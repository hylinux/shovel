from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.automation.domain import AutomationControl, ProactiveConfiguration, ProactiveService
from shovel.capability.domain import CapabilityScope
from shovel.db.model.automation import ProactiveService as ServiceRow
from shovel.db.model.retrieval import AppSetting
from shovel.db.model.runtime import Operation
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.domain.safe_data import ensure_no_credentials
from shovel.runtime.errors import NotFound, RuntimeFailure


def _service(row: ServiceRow) -> ProactiveService:
    return ProactiveService(
        id=row.id,
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        name=row.name,
        description=row.description,
        enabled=row.enabled,
        paused=row.paused,
        revision=row.revision,
        configuration=ProactiveConfiguration.model_validate(row.configuration),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SQLiteProactiveRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> ServiceRow:
        row = await session.scalar(
            select(ServiceRow).where(
                ServiceRow.id == identifier,
                ServiceRow.workspace_id == scope.workspace_id,
                ServiceRow.user_id == scope.user_id,
            )
        )
        if row is None:
            raise NotFound("Proactive service", identifier)
        return row

    async def create(
        self,
        scope: CapabilityScope,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool,
        name: str = "",
        description: str = "",
    ) -> ProactiveService:
        ensure_no_credentials(configuration)
        configuration = ProactiveConfiguration.model_validate(configuration.model_dump(mode="json"))
        async with sqlite_write(self.sessions) as session:
            now = self.clock()
            row = ServiceRow(
                id=new_id("proactive"),
                workspace_id=scope.workspace_id,
                user_id=scope.user_id,
                name=name,
                description=description,
                enabled=enabled,
                paused=False,
                revision=1,
                configuration=configuration.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
                schedule_cursor=now,
            )
            session.add(row)
            await session.commit()
            return _service(row)

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        paused: bool | None = None,
    ) -> ProactiveService:
        ensure_no_credentials(configuration)
        configuration = ProactiveConfiguration.model_validate(configuration.model_dump(mode="json"))
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.revision != expected_revision:
                raise RuntimeFailure("Proactive service revision conflict.", status=409)
            row.configuration, row.enabled = configuration.model_dump(mode="json"), enabled
            if name is not None:
                row.name = name
            if description is not None:
                row.description = description
            if paused is not None:
                row.paused = paused
            row.revision += 1
            row.updated_at = row.schedule_cursor = self.clock()
            await session.commit()
            return _service(row)

    async def get(self, scope: CapabilityScope, identifier: str) -> ProactiveService:
        async with self.sessions() as session:
            return _service(await self._find(session, scope, identifier))

    async def list(self, scope: CapabilityScope | None = None) -> list[ProactiveService]:
        statement = select(ServiceRow).order_by(ServiceRow.id)
        if scope is not None:
            statement = statement.where(
                ServiceRow.workspace_id == scope.workspace_id, ServiceRow.user_id == scope.user_id
            )
        async with self.sessions() as session:
            return [_service(row) for row in await session.scalars(statement)]

    async def cursor(self, service: ProactiveService) -> float:
        async with self.sessions() as session:
            row = await self._find(session, service.scope, service.id)
            if row.revision != service.revision:
                raise RuntimeFailure("Schedule revision changed.", status=409)
            return row.schedule_cursor

    async def checkpoint(self, service: ProactiveService, through: float) -> None:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, service.scope, service.id)
            if row.revision == service.revision:
                row.schedule_cursor = max(row.schedule_cursor, through)
                await session.commit()

    async def last_invoked(self, service: ProactiveService) -> float | None:
        admitted_at = cast(
            func.coalesce(
                func.json_extract(Operation.execution_data, "$.context.proactive_snapshot.admitted_at"),
                Operation.queued_at,
            ),
            Float,
        )
        statement = select(func.max(admitted_at)).where(
            Operation.execution_data.is_not(None),
            func.json_extract(Operation.execution_data, "$.context.scope.workspace_id")
            == service.scope.workspace_id,
            func.json_extract(Operation.execution_data, "$.context.scope.user_id") == service.scope.user_id,
            func.json_extract(Operation.execution_data, "$.context.proactive_snapshot.id") == service.id,
        )
        async with self.sessions() as session:
            last: float | None = await session.scalar(statement)
            return last

    @staticmethod
    def _control_key(scope: CapabilityScope) -> str:
        identity = json.dumps([scope.workspace_id, scope.user_id], ensure_ascii=False)
        return "automation.control." + hashlib.sha256(identity.encode()).hexdigest()

    async def control(self, scope: CapabilityScope) -> AutomationControl:
        async with self.sessions() as session:
            row = await session.get(AppSetting, self._control_key(scope))
            return (
                AutomationControl.model_validate_json(row.setting_value or "") if row else AutomationControl()
            )

    async def set_paused(
        self, scope: CapabilityScope, *, paused: bool, expected_revision: int
    ) -> AutomationControl:
        async with sqlite_write(self.sessions) as session:
            key = self._control_key(scope)
            row = await session.get(AppSetting, key)
            current = (
                AutomationControl.model_validate_json(row.setting_value or "") if row else AutomationControl()
            )
            if current.revision != expected_revision:
                raise RuntimeFailure("Automation control has changed; reload before continuing.", status=409)
            now = self.clock()
            updated = AutomationControl(
                paused=paused,
                revision=current.revision + 1,
                resumed_at=current.resumed_at if paused else now,
            )
            if row is None:
                session.add(AppSetting(setting_key=key, setting_value=updated.model_dump_json()))
            else:
                row.setting_value = updated.model_dump_json()
            if not paused:
                services = await session.scalars(
                    select(ServiceRow).where(
                        ServiceRow.workspace_id == scope.workspace_id,
                        ServiceRow.user_id == scope.user_id,
                    )
                )
                for service in services:
                    service.schedule_cursor = max(service.schedule_cursor, now)
            await session.commit()
            return updated
