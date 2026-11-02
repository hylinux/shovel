from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class EnterpriseConnectionProfile(Base):
    __tablename__ = "enterprise_connection_profile"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default="0")
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    manifest_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.CheckConstraint("enabled = 0", name="enterprise_connection_disabled"),
        sa.CheckConstraint("status IN ('draft', 'disabled')", name="enterprise_connection_state"),
        sa.CheckConstraint("revision >= 1", name="enterprise_connection_revision"),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at", name="enterprise_connection_times"
        ),
        sa.Index("idx_enterprise_connection_owner", "workspace_id", "user_id"),
    )


CONNECTION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS enterprise_connection_revision_guard
    BEFORE UPDATE ON enterprise_connection_profile
    WHEN NEW.id IS NOT OLD.id OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.user_id IS NOT OLD.user_id OR NEW.created_at IS NOT OLD.created_at
      OR NEW.revision != OLD.revision + 1
    BEGIN SELECT RAISE(ABORT, 'Connection changes require a new scoped revision'); END""",
)

for _statement in CONNECTION_DDL:
    event.listen(
        EnterpriseConnectionProfile.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
