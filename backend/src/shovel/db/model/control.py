import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, new_id


class ExecutionWakeup(Base):
    __tablename__ = "execution_wakeups"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("wakeup"))
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False
    )
    wake_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.CheckConstraint("state IN ('pending','applied','cancelled')", name="wakeup_state"),
        sa.CheckConstraint(
            "kind IN ('delay','snooze','retry','resume','recovery','child_ready')", name="wakeup_kind"
        ),
        sa.Index("idx_wakeup_due", "state", "wake_at"),
        sa.Index("uq_wakeup_pending", "execution_id", unique=True, sqlite_where=sa.text("state='pending'")),
    )


WAKEUP_DDL = (
    """CREATE TRIGGER IF NOT EXISTS wakeup_owner BEFORE INSERT ON execution_wakeups
    WHEN NEW.state!='pending' OR NOT EXISTS (SELECT 1 FROM operation
      WHERE id=NEW.execution_id AND execution_data IS NOT NULL AND state='suspended')
    BEGIN SELECT RAISE(ABORT,'Wakeup requires a suspended Execution'); END""",
    """CREATE TRIGGER IF NOT EXISTS wakeup_immutable BEFORE UPDATE ON execution_wakeups
    WHEN NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.kind IS NOT OLD.kind OR NEW.wake_at IS NOT OLD.wake_at
      OR NEW.created_at IS NOT OLD.created_at OR OLD.state!='pending'
      OR NEW.state NOT IN ('applied','cancelled')
    BEGIN SELECT RAISE(ABORT,'Wakeup fact is immutable'); END""",
)

for _statement in WAKEUP_DDL:
    event.listen(ExecutionWakeup.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
