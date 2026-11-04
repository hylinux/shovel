from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class EnterpriseSourceFence(Base):
    __tablename__ = "enterprise_source_fence"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    blocked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    event_id: Mapped[str | None] = mapped_column(sa.Text)
    event_digest: Mapped[str | None] = mapped_column(sa.Text)
    __table_args__ = (
        sa.CheckConstraint("generation >= 0", name="enterprise_source_generation"),
        sa.CheckConstraint(
            "(generation=0 AND blocked=0 AND event_id IS NULL AND event_digest IS NULL) OR "
            "(generation>=1 AND blocked=1 AND event_id IS NOT NULL AND event_digest IS NOT NULL)",
            name="enterprise_source_terminal_fence",
        ),
    )


class EnterpriseMetadata(Base):
    __tablename__ = "enterprise_metadata"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    metadata_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    admission_binding: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    vector_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    __table_args__ = (
        sa.CheckConstraint("revision >= 1", name="enterprise_metadata_revision"),
        sa.CheckConstraint("state IN ('active','invalid','deleted')", name="enterprise_metadata_state"),
        sa.Index("idx_enterprise_metadata_owner", "workspace_id", "user_id", "state"),
    )


class EnterpriseMetadataSource(Base):
    __tablename__ = "enterprise_metadata_source"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    metadata_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id", "metadata_id"],
            ["enterprise_metadata.workspace_id", "enterprise_metadata.user_id", "enterprise_metadata.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id", "source_key"],
            [
                "enterprise_source_fence.workspace_id",
                "enterprise_source_fence.user_id",
                "enterprise_source_fence.source_key",
            ],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("generation >= 0", name="enterprise_lineage_generation"),
        sa.Index("idx_enterprise_source_dependents", "workspace_id", "user_id", "source_key"),
    )


PROTECTION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_owner BEFORE INSERT ON enterprise_metadata
    WHEN NEW.state != 'active' OR NEW.revision != 1
      OR COALESCE(json_extract(NEW.payload,'$.id'),'') != NEW.id
      OR COALESCE(json_extract(NEW.payload,'$.scope.workspace_id'),'') != NEW.workspace_id
      OR COALESCE(json_extract(NEW.payload,'$.scope.user_id'),'') != NEW.user_id
    BEGIN SELECT RAISE(ABORT,'Enterprise metadata requires exact ownership'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_immutable BEFORE UPDATE ON enterprise_metadata
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.id IS NOT OLD.id
      OR NEW.metadata_digest IS NOT OLD.metadata_digest
      OR NEW.vector_id IS NOT OLD.vector_id
      OR NEW.revision != OLD.revision + 1
      OR NOT (
        ((OLD.state='active' AND NEW.state IN ('invalid','deleted'))
          OR (OLD.state='invalid' AND NEW.state='deleted'))
          AND NEW.payload IS OLD.payload AND NEW.admission_binding IS OLD.admission_binding
        OR NEW.state='deleted' AND NEW.payload='{}' AND NEW.admission_binding='{}'
          AND OLD.payload!='{}' AND EXISTS (SELECT 1 FROM enterprise_retention r
            WHERE r.workspace_id=OLD.workspace_id AND r.user_id=OLD.user_id
              AND r.metadata_id=OLD.id AND r.state='purged' AND r.retirement_digest IS NOT NULL))
    BEGIN SELECT RAISE(ABORT,'Enterprise metadata is immutable and cannot be resurrected'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_tombstone BEFORE DELETE ON enterprise_metadata
    BEGIN SELECT RAISE(ABORT,'Enterprise metadata requires a retained tombstone'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_no_replace BEFORE INSERT ON enterprise_metadata
    WHEN EXISTS (SELECT 1 FROM enterprise_metadata WHERE workspace_id=NEW.workspace_id
      AND user_id=NEW.user_id AND id=NEW.id)
    BEGIN SELECT RAISE(ABORT,'Enterprise metadata cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_source_fence_monotonic BEFORE UPDATE ON enterprise_source_fence
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.source_key IS NOT OLD.source_key OR NEW.blocked != 1
      OR NEW.generation != OLD.generation + 1
    BEGIN SELECT RAISE(ABORT,'Source fences cannot be rewound or reactivated'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_source_fence_tombstone BEFORE DELETE ON enterprise_source_fence
    BEGIN SELECT RAISE(ABORT,'Source revocation fences must survive deletion'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_source_fence_no_replace BEFORE INSERT
    ON enterprise_source_fence
    WHEN EXISTS (SELECT 1 FROM enterprise_source_fence WHERE workspace_id=NEW.workspace_id
      AND user_id=NEW.user_id AND source_key=NEW.source_key)
    BEGIN SELECT RAISE(ABORT,'Source revocation fences cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_source_current BEFORE INSERT
    ON enterprise_metadata_source
    WHEN NOT EXISTS (SELECT 1 FROM enterprise_source_fence f WHERE f.workspace_id=NEW.workspace_id
      AND f.user_id=NEW.user_id AND f.source_key=NEW.source_key
      AND f.generation=NEW.generation AND f.blocked=0)
    BEGIN SELECT RAISE(ABORT,'Enterprise lineage requires a current source generation'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_source_immutable BEFORE UPDATE
    ON enterprise_metadata_source
    BEGIN SELECT RAISE(ABORT,'Enterprise lineage is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_metadata_source_tombstone BEFORE DELETE
    ON enterprise_metadata_source
    BEGIN SELECT RAISE(ABORT,'Enterprise lineage must survive invalidation'); END""",
)

for _table, _name in (
    (EnterpriseMetadata.__table__, "enterprise_metadata"),
    (EnterpriseSourceFence.__table__, "enterprise_source_fence"),
    (EnterpriseMetadataSource.__table__, "enterprise_metadata_source"),
):
    for _statement in PROTECTION_DDL:
        if f" ON {_name}\n" in _statement:
            event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
