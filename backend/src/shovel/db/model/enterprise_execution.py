from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class AuthenticationCompletion(Base):
    __tablename__ = "authentication_completion"
    request_id: Mapped[str] = mapped_column(
        sa.ForeignKey("authentication_request.id", ondelete="RESTRICT"), primary_key=True
    )
    execution_id: Mapped[str] = mapped_column(sa.ForeignKey("operation.id", ondelete="RESTRICT"))
    binding_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operation_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    satisfied_at: Mapped[float] = mapped_column(sa.Float, nullable=False)


class ProtectionAudit(Base):
    __tablename__ = "protection_audit"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    execution_id: Mapped[str] = mapped_column(sa.ForeignKey("operation.id", ondelete="RESTRICT"))
    event: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)


ENTERPRISE_EXECUTION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS authentication_completion_valid BEFORE INSERT ON authentication_completion
    WHEN NOT EXISTS (SELECT 1 FROM authentication_request a JOIN operation o ON a.execution_id=o.id
      WHERE a.id=NEW.request_id AND a.execution_id=NEW.execution_id AND a.state='pending'
      AND a.binding_digest=NEW.binding_digest AND o.state='running'
      AND json_extract(o.execution_control,'$.authentication.request_id')=NEW.request_id
      AND json_extract(o.execution_control,'$.authentication.operation_digest')=NEW.operation_digest
      AND json_extract(o.execution_control,'$.enterprise_operation_digest')=NEW.operation_digest
      AND a.created_at<=NEW.satisfied_at AND NEW.satisfied_at<a.expires_at
      AND o.execution_deadline=a.expires_at)
    BEGIN SELECT RAISE(ABORT,'Authentication completion requires a current running execution'); END""",
    """CREATE TRIGGER IF NOT EXISTS authentication_completion_immutable BEFORE UPDATE
    ON authentication_completion
    BEGIN SELECT RAISE(ABORT,'Authentication completion is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS authentication_completion_retained BEFORE DELETE
    ON authentication_completion
    BEGIN SELECT RAISE(ABORT,'Authentication completion must be retained'); END""",
    """CREATE TRIGGER IF NOT EXISTS protection_audit_owner BEFORE INSERT ON protection_audit
    WHEN NEW.id IS NOT json_extract(NEW.event,'$.audit_ref') OR NOT EXISTS
      (SELECT 1 FROM operation WHERE id=NEW.execution_id
      AND json_extract(execution_data,'$.context.scope.workspace_id')=NEW.workspace_id
      AND json_extract(execution_data,'$.context.scope.user_id')=NEW.user_id)
    BEGIN SELECT RAISE(ABORT,'Protection audit requires exact execution ownership'); END""",
    """CREATE TRIGGER IF NOT EXISTS protection_audit_immutable BEFORE UPDATE ON protection_audit
    BEGIN SELECT RAISE(ABORT,'Protection audit is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS protection_audit_retained BEFORE DELETE ON protection_audit
    BEGIN SELECT RAISE(ABORT,'Protection audit must be retained'); END""",
)

for _table, _name in (
    (AuthenticationCompletion.__table__, "authentication_completion"),
    (ProtectionAudit.__table__, "protection_audit"),
):
    for _statement in ENTERPRISE_EXECUTION_DDL:
        if f" ON {_name}\n" in _statement:
            event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
