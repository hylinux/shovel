from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONList, new_id


class ApprovalRequest(Base):
    __tablename__ = "approval_request"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("approval"))
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    binding_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONList, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    decided_at: Mapped[float | None] = mapped_column(sa.Float)
    consumed_at: Mapped[float | None] = mapped_column(sa.Float)
    consumed_attempt: Mapped[int | None] = mapped_column(sa.Integer)

    __table_args__ = (
        sa.UniqueConstraint("execution_id", "number", name="uq_approval_execution_number"),
        sa.CheckConstraint("number > 0 AND expires_at > created_at", name="approval_expiration"),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','expired','consumed')",
            name="approval_state",
        ),
        sa.CheckConstraint(
            "(state = 'consumed' AND consumed_at IS NOT NULL "
            "AND consumed_attempt IS NOT NULL AND consumed_attempt > 0) OR "
            "(state != 'consumed' AND consumed_at IS NULL AND consumed_attempt IS NULL)",
            name="approval_consumption",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL) OR (state != 'pending' AND decided_at IS NOT NULL)",
            name="approval_decision_time",
        ),
        sa.Index("idx_approval_owner", "workspace_id", "user_id", "state"),
        sa.Index("idx_approval_expiration", "state", "expires_at"),
        sa.Index(
            "uq_approval_consumed_attempt",
            "execution_id",
            "consumed_attempt",
            unique=True,
            sqlite_where=sa.text("state='consumed'"),
        ),
    )


APPROVAL_DDL = (
    """CREATE TRIGGER IF NOT EXISTS approval_no_replace
    BEFORE INSERT ON approval_request WHEN EXISTS
      (SELECT 1 FROM approval_request WHERE id = NEW.id
       OR (execution_id = NEW.execution_id AND number = NEW.number))
    BEGIN SELECT RAISE(ABORT, 'Approval receipts cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS approval_insert_valid
    BEFORE INSERT ON approval_request WHEN NEW.state != 'pending'
      OR NOT EXISTS (SELECT 1 FROM operation
        WHERE id = NEW.execution_id AND execution_data IS NOT NULL
          AND json_extract(execution_data, '$.context.scope.workspace_id') = NEW.workspace_id
          AND json_extract(execution_data, '$.context.scope.user_id') = NEW.user_id)
    BEGIN SELECT RAISE(ABORT, 'Approval owner or execution is invalid'); END""",
    """CREATE TRIGGER IF NOT EXISTS approval_identity_immutable
    BEFORE UPDATE ON approval_request WHEN
      NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.number IS NOT OLD.number OR NEW.binding_hash IS NOT OLD.binding_hash
      OR NEW.reasons IS NOT OLD.reasons OR NEW.created_at IS NOT OLD.created_at
      OR NEW.expires_at IS NOT OLD.expires_at
      OR (OLD.decided_at IS NOT NULL AND NEW.decided_at IS NOT OLD.decided_at)
      OR (OLD.state IN ('consumed','rejected','expired') AND
          (NEW.decided_at IS NOT OLD.decided_at OR NEW.consumed_at IS NOT OLD.consumed_at
           OR NEW.consumed_attempt IS NOT OLD.consumed_attempt))
    BEGIN SELECT RAISE(ABORT, 'Approval binding is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS approval_state_transition
    BEFORE UPDATE OF state ON approval_request WHEN NEW.state != OLD.state AND NOT
      ((OLD.state = 'pending' AND NEW.state IN ('approved','rejected','expired'))
       OR (OLD.state = 'approved' AND NEW.state IN ('consumed','expired')))
    BEGIN SELECT RAISE(ABORT, 'Illegal approval state transition'); END""",
    """CREATE TRIGGER IF NOT EXISTS approval_consumption_attempt
    BEFORE UPDATE ON approval_request WHEN NEW.state='consumed' AND NOT EXISTS
      (SELECT 1 FROM execution_attempt WHERE execution_id=NEW.execution_id AND number=NEW.consumed_attempt)
    BEGIN SELECT RAISE(ABORT, 'Approval consumption requires its execution attempt'); END""",
)

for _statement in APPROVAL_DDL:
    event.listen(
        ApprovalRequest.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
