from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict, JSONEncoded, JSONList, new_id


class InputRequest(Base):
    __tablename__ = "input_request"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    prompt: Mapped[str] = mapped_column(sa.Text, nullable=False)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    response: Mapped[Any] = mapped_column(JSONEncoded, nullable=True)
    expires_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.UniqueConstraint("execution_id", "node_id", name="uq_input_request_node"),
        sa.CheckConstraint("state IN ('pending','answered','consumed','expired')", name="interaction_state"),
        sa.Index("idx_input_owner", "workspace_id", "user_id", "state"),
        sa.Index("idx_input_expiration", "state", "expires_at"),
    )


class Interaction(Base):
    __tablename__ = "interaction"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actions: Mapped[list[str]] = mapped_column(JSONList, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="open")
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    snoozed_until: Mapped[float | None] = mapped_column(sa.Float)
    __table_args__ = (
        sa.UniqueConstraint("source_kind", "source_id", name="uq_interaction_source"),
        sa.CheckConstraint(
            "kind IN ('information','approval','input_request','warning','result')", name="interaction_kind"
        ),
        sa.CheckConstraint("state IN ('open','dismissed','resolved')", name="projection_state"),
        sa.Index("idx_interaction_scope", "workspace_id", "user_id", "state"),
    )


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    __table_args__ = (sa.UniqueConstraint("execution_id", "node_id", name="uq_notification_node"),)


INPUT_DDL = (
    """CREATE TRIGGER IF NOT EXISTS input_request_owner BEFORE INSERT ON input_request
    WHEN NEW.state != 'pending' OR NOT EXISTS (SELECT 1 FROM operation WHERE id=NEW.execution_id
      AND json_extract(execution_data,'$.capability.kind')='workflow'
      AND json_extract(execution_data,'$.context.scope.workspace_id')=NEW.workspace_id
      AND json_extract(execution_data,'$.context.scope.user_id')=NEW.user_id)
    BEGIN SELECT RAISE(ABORT,'Interaction owner is invalid'); END""",
    """CREATE TRIGGER IF NOT EXISTS input_request_no_replace BEFORE INSERT ON input_request
    WHEN EXISTS (SELECT 1 FROM input_request WHERE id=NEW.id OR
      (execution_id=NEW.execution_id AND node_id=NEW.node_id))
    BEGIN SELECT RAISE(ABORT,'Interaction cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS input_request_immutable BEFORE UPDATE ON input_request
    WHEN NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.node_id IS NOT OLD.node_id OR NEW.prompt IS NOT OLD.prompt
      OR NEW.response_schema IS NOT OLD.response_schema OR NEW.expires_at IS NOT OLD.expires_at
      OR (NOT (OLD.state='pending' AND NEW.state='answered') AND NEW.response IS NOT OLD.response)
      OR NOT (NEW.state=OLD.state OR (OLD.state='pending' AND NEW.state IN ('answered','expired'))
              OR (OLD.state='answered' AND NEW.state='consumed'))
    BEGIN SELECT RAISE(ABORT,'Interaction binding or response is immutable'); END""",
)

for _statement in INPUT_DDL:
    event.listen(InputRequest.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]

NOTIFICATION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS notification_owner BEFORE INSERT ON notification
    WHEN NOT EXISTS (SELECT 1 FROM operation WHERE id=NEW.execution_id
      AND execution_data IS NOT NULL
      AND json_extract(execution_data,'$.context.scope.workspace_id')=NEW.workspace_id
      AND json_extract(execution_data,'$.context.scope.user_id')=NEW.user_id)
    BEGIN SELECT RAISE(ABORT,'Notification owner is invalid'); END""",
    """CREATE TRIGGER IF NOT EXISTS notification_no_replace BEFORE INSERT ON notification
    WHEN EXISTS (SELECT 1 FROM notification WHERE id=NEW.id OR
      (execution_id=NEW.execution_id AND node_id=NEW.node_id))
    BEGIN SELECT RAISE(ABORT,'Notification cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS notification_immutable BEFORE UPDATE ON notification
    BEGIN SELECT RAISE(ABORT,'Notification content is immutable'); END""",
)

for _statement in NOTIFICATION_DDL:
    event.listen(Notification.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]

INTERACTION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS projection_owner BEFORE INSERT ON interaction
    WHEN NOT EXISTS (SELECT 1 FROM operation WHERE id=NEW.execution_id AND execution_data IS NOT NULL
      AND json_extract(execution_data,'$.context.scope.workspace_id')=NEW.workspace_id
      AND json_extract(execution_data,'$.context.scope.user_id')=NEW.user_id)
    BEGIN SELECT RAISE(ABORT,'Interaction projection owner is invalid'); END""",
    """CREATE TRIGGER IF NOT EXISTS projection_identity BEFORE UPDATE ON interaction
    WHEN NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.workspace_id IS NOT OLD.workspace_id OR NEW.user_id IS NOT OLD.user_id
      OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_id IS NOT OLD.source_id
      OR NEW.kind IS NOT OLD.kind OR NEW.message IS NOT OLD.message OR NEW.created_at IS NOT OLD.created_at
    BEGIN SELECT RAISE(ABORT,'Interaction projection identity is immutable'); END""",
)

for _statement in INTERACTION_DDL:
    event.listen(Interaction.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]


class NotificationDelivery(Base):
    __tablename__ = "notification_delivery"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("delivery"))
    notification_id: Mapped[str] = mapped_column(
        sa.ForeignKey("notification.id", ondelete="RESTRICT"), nullable=False
    )
    interaction_id: Mapped[str] = mapped_column(
        sa.ForeignKey("interaction.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    next_at: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0)
    token: Mapped[str | None] = mapped_column(sa.Text)
    delivered_at: Mapped[float | None] = mapped_column(sa.Float)
    error: Mapped[str | None] = mapped_column(sa.Text)
    __table_args__ = (
        sa.UniqueConstraint("notification_id", "channel", name="uq_notification_channel"),
        sa.CheckConstraint("channel IN ('in_app','console')", name="delivery_channel"),
        sa.CheckConstraint("state IN ('pending','delivering','delivered','failed')", name="delivery_state"),
        sa.Index("idx_delivery_due", "state", "next_at"),
    )
