import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from shovel.capability.domain import CapabilityScope
from shovel.db.model.enterprise_execution import ProtectionAudit as AuditRow
from shovel.protection.domain import ProtectionAdapterError
from shovel.protection.policy import PolicyAudit


@dataclass(frozen=True)
class _AuditUnit:
    session: AsyncSession
    scope: CapabilityScope
    execution_id: str
    task: asyncio.Task[object] | None


class SQLitePolicyAudit:
    """Stage allowlisted decision facts in the caller's explicit SQLite transaction."""

    def __init__(self) -> None:
        self._unit: ContextVar[_AuditUnit | None] = ContextVar("protection_audit_unit", default=None)

    @contextmanager
    def bind(self, session: AsyncSession, scope: CapabilityScope, execution_id: str) -> Iterator[None]:
        token = self._unit.set(_AuditUnit(session, scope, execution_id, asyncio.current_task()))
        try:
            yield
        finally:
            self._unit.reset(token)

    def record(self, event: PolicyAudit) -> None:
        event = PolicyAudit.model_validate(event)
        unit = self._unit.get()
        if unit is None or unit.task is not asyncio.current_task():
            raise ProtectionAdapterError("A protection audit transaction is required.")
        unit.session.add(
            AuditRow(
                id=event.audit_ref,
                workspace_id=unit.scope.workspace_id,
                user_id=unit.scope.user_id,
                execution_id=unit.execution_id,
                event=event.model_dump(mode="json"),
            )
        )
