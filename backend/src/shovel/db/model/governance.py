import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base


class EnterpriseRetention(Base):
    __tablename__ = "enterprise_retention"
    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    metadata_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    policy_digest: Mapped[str] = mapped_column(sa.Text, nullable=False)
    store_binding: Mapped[str] = mapped_column(sa.Text, nullable=False)
    storage_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    key_authority_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    authority_generation: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    admitted_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    retirement_digest: Mapped[str | None] = mapped_column(sa.Text)
    purged_at: Mapped[float | None] = mapped_column(sa.Float)
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id", "metadata_id"],
            ["enterprise_metadata.workspace_id", "enterprise_metadata.user_id", "enterprise_metadata.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("authority_generation >= 0", name="enterprise_retention_generation"),
        sa.CheckConstraint("expires_at > admitted_at", name="enterprise_retention_deadline"),
        sa.CheckConstraint(
            "(state='active' AND retirement_digest IS NULL AND purged_at IS NULL) OR "
            "(state='purged' AND retirement_digest IS NOT NULL AND purged_at IS NOT NULL)",
            name="enterprise_retention_state",
        ),
    )


GOVERNANCE_DDL = (
    """CREATE TRIGGER IF NOT EXISTS enterprise_retention_immutable BEFORE UPDATE ON enterprise_retention
    WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.metadata_id IS NOT OLD.metadata_id OR NEW.policy_digest IS NOT OLD.policy_digest
      OR NEW.store_binding IS NOT OLD.store_binding
      OR NEW.storage_id IS NOT OLD.storage_id OR NEW.key_authority_id IS NOT OLD.key_authority_id
      OR NEW.authority_generation IS NOT OLD.authority_generation
      OR NEW.admitted_at IS NOT OLD.admitted_at OR NEW.expires_at IS NOT OLD.expires_at
      OR OLD.state != 'active' OR NEW.state != 'purged'
      OR NEW.retirement_digest IS NULL OR NEW.purged_at IS NULL
    BEGIN SELECT RAISE(ABORT,'Enterprise retention cannot be extended or reactivated'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_retention_retained BEFORE DELETE ON enterprise_retention
    BEGIN SELECT RAISE(ABORT,'Enterprise retirement evidence must survive deletion'); END""",
    """CREATE TRIGGER IF NOT EXISTS enterprise_retention_no_replace BEFORE INSERT ON enterprise_retention
    WHEN NEW.state!='active' OR EXISTS (SELECT 1 FROM enterprise_retention
      WHERE workspace_id=NEW.workspace_id AND user_id=NEW.user_id AND metadata_id=NEW.metadata_id)
    BEGIN SELECT RAISE(ABORT,'Enterprise retention cannot be replaced'); END""",
)

for _statement in GOVERNANCE_DDL:
    event.listen(
        EnterpriseRetention.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
