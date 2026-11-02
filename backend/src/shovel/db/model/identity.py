from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class IdentityProfile(Base):
    __tablename__ = "identity_profile"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.CheckConstraint("revision >= 1", name="identity_revision"),
        sa.CheckConstraint("created_at >= 0 AND updated_at >= created_at", name="identity_times"),
        sa.Index("idx_identity_owner", "workspace_id", "user_id"),
    )


class AuthenticationRequest(Base):
    __tablename__ = "authentication_request"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(sa.ForeignKey("operation.id", ondelete="RESTRICT"))
    identity_profile_id: Mapped[str] = mapped_column(
        sa.ForeignKey("identity_profile.id", ondelete="RESTRICT")
    )
    connection_profile_id: Mapped[str] = mapped_column(
        sa.ForeignKey("enterprise_connection_profile.id", ondelete="RESTRICT"),
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    binding_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    requires_fresh_challenge: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.UniqueConstraint("execution_id", "binding_digest", name="uq_authentication_binding"),
        sa.CheckConstraint("state IN ('pending','cancelled','expired')", name="authentication_state"),
        sa.CheckConstraint("expires_at > created_at", name="authentication_deadline"),
        sa.Index("idx_authentication_owner", "workspace_id", "user_id", "state"),
    )


IDENTITY_DDL = (
    """CREATE TRIGGER IF NOT EXISTS identity_revision_guard BEFORE UPDATE ON identity_profile
    WHEN NEW.id IS NOT OLD.id OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.user_id IS NOT OLD.user_id OR NEW.created_at IS NOT OLD.created_at
      OR NEW.revision != OLD.revision + 1
    BEGIN SELECT RAISE(ABORT,'Identity configuration requires a scoped revision'); END""",
)
AUTHENTICATION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS authentication_owner BEFORE INSERT ON authentication_request
    WHEN NEW.state != 'pending' OR NOT EXISTS (SELECT 1 FROM operation WHERE id=NEW.execution_id
      AND state='suspended' AND execution_deadline=NEW.expires_at
      AND json_extract(execution_data,'$.context.scope.workspace_id')=NEW.workspace_id
      AND json_extract(execution_data,'$.context.scope.user_id')=NEW.user_id)
    BEGIN SELECT RAISE(ABORT,'Authentication request requires a bounded suspended execution'); END""",
    """CREATE TRIGGER IF NOT EXISTS authentication_immutable BEFORE UPDATE ON authentication_request
    WHEN NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.identity_profile_id IS NOT OLD.identity_profile_id
      OR NEW.connection_profile_id IS NOT OLD.connection_profile_id
      OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.request IS NOT OLD.request OR NEW.binding_digest IS NOT OLD.binding_digest
      OR NEW.reason IS NOT OLD.reason OR NEW.requires_fresh_challenge IS NOT OLD.requires_fresh_challenge
      OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
      OR NOT (NEW.state=OLD.state OR (OLD.state='pending' AND NEW.state IN ('cancelled','expired')))
    BEGIN SELECT RAISE(ABORT,'Authentication binding and deadline are immutable'); END""",
)

for _table, _statements in (
    (IdentityProfile.__table__, IDENTITY_DDL),
    (AuthenticationRequest.__table__, AUTHENTICATION_DDL),
):
    for _statement in _statements:
        event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
