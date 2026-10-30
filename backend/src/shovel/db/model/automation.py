from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class ProactiveService(Base):
    __tablename__ = "proactive_service"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    paused: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default="0")
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    schedule_cursor: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.CheckConstraint("revision > 0", name="proactive_revision"),
        sa.Index("idx_proactive_owner", "workspace_id", "user_id", "enabled"),
    )


PROACTIVE_DDL = (
    """CREATE TRIGGER IF NOT EXISTS proactive_revision_guard BEFORE UPDATE ON proactive_service
    WHEN NEW.id IS NOT OLD.id OR NEW.workspace_id IS NOT OLD.workspace_id
      OR NEW.user_id IS NOT OLD.user_id OR NEW.created_at IS NOT OLD.created_at
      OR NEW.revision NOT IN (OLD.revision, OLD.revision + 1)
      OR ((NEW.configuration IS NOT OLD.configuration OR NEW.enabled IS NOT OLD.enabled
          OR NEW.name IS NOT OLD.name OR NEW.description IS NOT OLD.description
          OR NEW.paused IS NOT OLD.paused)
          AND NEW.revision != OLD.revision + 1)
    BEGIN SELECT RAISE(ABORT, 'Proactive configuration requires a new revision'); END""",
)

for _statement in PROACTIVE_DDL:
    event.listen(
        ProactiveService.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
