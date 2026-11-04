import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict


class OrganizationRevision(Base):
    __tablename__ = "organization_revision"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    organization_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    policy_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    __table_args__ = (sa.CheckConstraint("revision >= 1", name="organization_positive_revision"),)


class OrganizationSource(Base):
    __tablename__ = "organization_source"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    organization_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    connection_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    resource: Mapped[dict[str, object]] = mapped_column(JSONDict, nullable=False)
    policy_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    blocked: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    observed_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (sa.CheckConstraint("generation >= 0", name="organization_source_generation"),)


ORGANIZATION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS organization_revision_monotonic BEFORE UPDATE ON organization_revision
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.organization_id IS NOT OLD.organization_id OR NEW.revision <= OLD.revision
    BEGIN SELECT RAISE(ABORT,'Organization policy revisions cannot be rewound or reused'); END""",
    """CREATE TRIGGER IF NOT EXISTS organization_revision_tombstone BEFORE DELETE ON organization_revision
    BEGIN SELECT RAISE(ABORT,'Organization policy revision history must survive'); END""",
    """CREATE TRIGGER IF NOT EXISTS organization_source_monotonic BEFORE UPDATE ON organization_source
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.organization_id IS NOT OLD.organization_id OR NEW.connection_id IS NOT OLD.connection_id
      OR NEW.resource IS NOT OLD.resource OR (OLD.blocked=1 AND NEW.blocked!=1)
      OR NEW.generation < OLD.generation OR NEW.observed_at < OLD.observed_at
    BEGIN SELECT RAISE(ABORT,'Organization source cannot be rebound or reactivated'); END""",
    """CREATE TRIGGER IF NOT EXISTS organization_source_tombstone BEFORE DELETE ON organization_source
    BEGIN SELECT RAISE(ABORT,'Organization source tombstones must survive'); END""",
)

for _table, _prefix in (
    (OrganizationRevision.__table__, "organization_revision"),
    (OrganizationSource.__table__, "organization_source"),
):
    for _statement in ORGANIZATION_DDL:
        if f" ON {_prefix}\n" in _statement:
            event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
