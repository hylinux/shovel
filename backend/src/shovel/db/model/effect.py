import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy import DDL, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict
from shovel.domain.identity import new_id


class ExternalEffect(Base):
    __tablename__ = "external_effect"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        sa.ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    intent: Mapped[dict[str, JsonValue]] = mapped_column(JSONDict, nullable=False)
    fingerprint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    effect_key: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    claim_token: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resume_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    conflicted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(sa.Text)
    reconciliation_token: Mapped[str | None] = mapped_column(sa.Text)
    reconciliation_until: Mapped[float | None] = mapped_column(sa.Float)
    updated_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (
        sa.UniqueConstraint("execution_id"),
        sa.ForeignKeyConstraint(
            ["execution_id", "attempt"],
            ["execution_attempt.execution_id", "execution_attempt.number"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('intent','dispatched','uncertain','not_sent','not_applied','applied')",
            name="external_effect_state",
        ),
        sa.CheckConstraint("revision > 0 AND attempt > 0", name="external_effect_revision"),
    )


class ExternalEffectEvent(Base):
    __tablename__ = "external_effect_event"
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    effect_id: Mapped[str] = mapped_column(
        sa.ForeignKey("external_effect.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_attempt: Mapped[int | None] = mapped_column(sa.Integer)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    __table_args__ = (sa.UniqueConstraint("effect_id", "revision"),)


def record_effect(
    session: AsyncSession, row: ExternalEffect, now: float, kind: str, *, source_attempt: int | None = None
) -> None:
    row.revision += 1
    row.updated_at = now
    session.add(
        ExternalEffectEvent(
            id=new_id("effect_event"),
            effect_id=row.id,
            revision=row.revision,
            attempt=row.attempt,
            kind=kind,
            source_attempt=source_attempt,
            state=row.state,
            evidence_digest=row.evidence_digest,
            created_at=now,
        )
    )


EFFECT_DDL = (
    """CREATE TRIGGER IF NOT EXISTS external_effect_no_replace BEFORE INSERT ON external_effect
    WHEN EXISTS (SELECT 1 FROM external_effect WHERE id=NEW.id OR execution_id=NEW.execution_id
      OR idempotency_key=NEW.idempotency_key)
    BEGIN SELECT RAISE(ABORT,'External effect receipts cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_owner BEFORE INSERT ON external_effect
    WHEN NEW.state != 'intent' OR NEW.revision != 1 OR NOT EXISTS
      (SELECT 1 FROM operation o JOIN execution_attempt a ON a.execution_id=o.id
      WHERE o.id=NEW.execution_id AND o.state='running' AND o.claim_token=NEW.claim_token
      AND a.claim_token=NEW.claim_token AND a.number=NEW.attempt AND a.finished_at IS NULL
      AND a.dispatched_at IS NOT NULL)
    BEGIN SELECT RAISE(ABORT,'External effect requires an owned dispatched attempt'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_binding BEFORE UPDATE ON external_effect
    WHEN NEW.id IS NOT OLD.id OR NEW.execution_id IS NOT OLD.execution_id
      OR NEW.intent IS NOT OLD.intent OR NEW.fingerprint IS NOT OLD.fingerprint
      OR NEW.effect_key IS NOT OLD.effect_key
      OR (OLD.conflicted=1 AND NEW.conflicted!=1)
      OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.revision != OLD.revision+1
    BEGIN SELECT RAISE(ABORT,'External effect binding is immutable and revisioned'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_retained BEFORE DELETE ON external_effect
    BEGIN SELECT RAISE(ABORT,'External effect receipts must be retained'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_event_immutable BEFORE UPDATE ON external_effect_event
    BEGIN SELECT RAISE(ABORT,'External effect events are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_event_no_replace BEFORE INSERT ON external_effect_event
    WHEN EXISTS (SELECT 1 FROM external_effect_event WHERE id=NEW.id
      OR (effect_id=NEW.effect_id AND revision=NEW.revision))
    BEGIN SELECT RAISE(ABORT,'External effect events cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS external_effect_event_retained BEFORE DELETE ON external_effect_event
    BEGIN SELECT RAISE(ABORT,'External effect events must be retained'); END""",
)

for _table, _name in (
    (ExternalEffect.__table__, "external_effect"),
    (ExternalEffectEvent.__table__, "external_effect_event"),
):
    for _statement in EFFECT_DDL:
        if f" ON {_name}\n" in _statement:
            event.listen(_table, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]
