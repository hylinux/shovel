"""Domain B — execution: job, job_event."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, ForeignKey, Index, event, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import ErrorKind, JobState, LogLevel, ScanMode, ScheduleKind, Stage
from ...execution.domain import ExecutionState, ExecutionStateMachine
from ..base import Base, Enum, JSONDict, new_id, now_ts


class Job(Base):
    """One concrete execution. ``job.id`` doubles as the sweep run id that
    ``Document.last_seen_run`` points at -- that is what makes the observed-set
    diff a single UPDATE instead of an in-memory set difference."""

    __tablename__ = "job"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("job"))
    scan_profile_id: Mapped[str] = mapped_column(
        ForeignKey("scan_profile.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"), nullable=False)
    schedule_id: Mapped[str | None] = mapped_column(sa.Text)  # NULL for manual runs
    fired_by_kind: Mapped[ScheduleKind] = mapped_column(Enum(ScheduleKind), nullable=False)
    scan_mode: Mapped[ScanMode] = mapped_column(Enum(ScanMode), nullable=False)

    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.QUEUED, nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, default=100, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(sa.Text)

    queued_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    started_at: Mapped[int | None] = mapped_column(sa.Integer)
    finished_at: Mapped[int | None] = mapped_column(sa.Integer)
    heartbeat_at: Mapped[int | None] = mapped_column(sa.Integer)
    worker_id: Mapped[str | None] = mapped_column(sa.Text)

    # per-stage counters; `discovered` is the progress-bar denominator
    discovered: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    skipped_unchanged: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    fetched: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    parsed: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    chunked: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    embedded: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    failed_docs: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    chunks_written: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    bytes_read: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    current_stage: Mapped[Stage | None] = mapped_column(Enum(Stage))
    #: HARD GATE for deletion propagation. An aborted sweep must never be allowed
    #: to conclude "everything vanished" and wipe the user's index.
    is_discovery_complete: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    is_cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    cursor_out: Mapped[str | None] = mapped_column(sa.Text)

    error_kind: Mapped[ErrorKind | None] = mapped_column(Enum(ErrorKind))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    stats: Mapped[dict[str, Any]] = mapped_column("stats_json", JSONDict, default=dict, nullable=False)

    events: Mapped[list[JobEvent]] = relationship(back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_job_queue", "priority", "queued_at", sqlite_where=text("state = 'queued'")),
        # "is a sibling job of the same kind already running?" -- prevents the
        # slow-scan + fast-cadence task avalanche
        Index(
            "idx_job_active", "resource_id", "scan_mode", sqlite_where=text("state IN ('queued','running')")
        ),
        Index("idx_job_zombie", "heartbeat_at", sqlite_where=text("state = 'running'")),
        Index("idx_job_hist", "resource_id", "queued_at"),
    )

    @property
    def progress(self) -> float:
        """Completed work fraction, with successful terminal jobs at 1.0."""
        if self.state in (JobState.SUCCEEDED, JobState.COMPLETED_WITH_ERRORS):
            return 1.0
        if not self.is_discovery_complete or self.discovered == 0:
            return 0.0
        processed = self.chunked if self.stats.get("embedding_enabled") is False else self.embedded
        done = processed + self.skipped_unchanged + self.failed_docs
        return min(1.0, done / self.discovered)

    @property
    def may_reconcile_deletions(self) -> bool:
        return (
            self.scan_mode == ScanMode.FULL_SWEEP
            and self.is_discovery_complete
            and self.stats.get("delete_detect") is True
        )

    def terminal_state(self) -> JobState:
        """`partial` is a first-class outcome: "982 of 1000 succeeded" is neither
        success nor failure, and flagging the whole job red would mislead."""
        if self.is_cancel_requested:
            return JobState.CANCELLED
        if self.error_kind is not None:
            return JobState.FAILED
        return JobState.COMPLETED_WITH_ERRORS if self.failed_docs else JobState.SUCCEEDED


class JobEvent(Base):
    """Structured, user-visible log line. Debug-level logging goes to files."""

    __tablename__ = "job_event"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), nullable=False)
    logged_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    level: Mapped[LogLevel] = mapped_column(Enum(LogLevel), default=LogLevel.INFO, nullable=False)
    stage: Mapped[Stage | None] = mapped_column(Enum(Stage))
    doc_uri: Mapped[str | None] = mapped_column(sa.Text)
    code: Mapped[str | None] = mapped_column(sa.Text)  # PARSE_ENCRYPTED_PDF, ...
    message: Mapped[str | None] = mapped_column(sa.Text)
    detail: Mapped[dict[str, Any]] = mapped_column("detail_json", JSONDict, default=dict, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_jobevent", "job_id", "logged_at"),
        Index("idx_jobevent_err", "job_id", "code", sqlite_where=text("level = 'error'")),
    )


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempt"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("attempt"))
    execution_id: Mapped[str] = mapped_column(ForeignKey("operation.id", ondelete="RESTRICT"), nullable=False)
    number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    claim_token: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    started_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    dispatched_at: Mapped[float | None] = mapped_column(sa.Float)
    finished_at: Mapped[float | None] = mapped_column(sa.Float)
    outcome: Mapped[str | None] = mapped_column(sa.Text)
    reason: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.UniqueConstraint("execution_id", "number", name="uq_execution_attempt_number"),
        sa.CheckConstraint("number > 0", name="execution_attempt_positive"),
    )


class ExecutionStep(Base):
    """One gated capability dispatch, not a workflow node or another lifecycle owner."""

    __tablename__ = "execution_step"

    execution_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    attempt_number: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, default="running")
    started_at: Mapped[float] = mapped_column(sa.Float, nullable=False)
    finished_at: Mapped[float | None] = mapped_column(sa.Float)

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["execution_id", "attempt_number"],
            ["execution_attempt.execution_id", "execution_attempt.number"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('running','suspended','succeeded','failed','cancelled')",
            name="execution_step_state",
        ),
        sa.CheckConstraint(
            "(state='running' AND finished_at IS NULL) OR (state!='running' AND finished_at IS NOT NULL)",
            name="execution_step_completion",
        ),
    )


STEP_DDL = (
    """CREATE TRIGGER IF NOT EXISTS execution_step_insert_valid BEFORE INSERT ON execution_step
    WHEN NEW.state != 'running' OR NOT EXISTS
      (SELECT 1 FROM execution_attempt WHERE execution_id=NEW.execution_id AND number=NEW.attempt_number
       AND dispatched_at IS NOT NULL AND finished_at IS NULL)
    BEGIN SELECT RAISE(ABORT, 'Execution step requires a dispatched attempt'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_step_immutable BEFORE UPDATE ON execution_step
    WHEN NEW.execution_id IS NOT OLD.execution_id OR NEW.attempt_number IS NOT OLD.attempt_number
      OR NEW.started_at IS NOT OLD.started_at OR OLD.state != 'running'
    BEGIN SELECT RAISE(ABORT, 'Execution step fact is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_step_no_replace BEFORE INSERT ON execution_step
    WHEN EXISTS (SELECT 1 FROM execution_step
      WHERE execution_id=NEW.execution_id AND attempt_number=NEW.attempt_number)
    BEGIN SELECT RAISE(ABORT, 'Execution step cannot be replaced'); END""",
)

for _statement in STEP_DDL:
    event.listen(
        ExecutionStep.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )


EXECUTION_DDL = (
    """CREATE TRIGGER IF NOT EXISTS execution_no_replace
    BEFORE INSERT ON operation WHEN EXISTS
      (SELECT 1 FROM operation WHERE execution_data IS NOT NULL
       AND (id = NEW.id OR request_key = NEW.request_key
            OR (parent_execution_id = NEW.parent_execution_id AND child_key = NEW.child_key)))
    BEGIN SELECT RAISE(ABORT, 'Execution receipts cannot be replaced'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_insert_valid
    BEFORE INSERT ON operation
    WHEN ((NEW.capability_version_id IS NOT NULL) != (NEW.execution_data IS NOT NULL))
      OR (NEW.kind = 'capability.invoke' AND NEW.execution_data IS NULL)
      OR (NEW.execution_data IS NOT NULL AND
          (NEW.kind != 'capability.invoke' OR NEW.root_execution_id IS NULL OR NEW.correlation_id IS NULL
           OR NEW.state != 'queued'))
    BEGIN SELECT RAISE(ABORT, 'Invalid versioned execution identity'); END""",
    """CREATE TRIGGER IF NOT EXISTS execution_snapshot_immutable
    BEFORE UPDATE ON operation WHEN OLD.execution_data IS NOT NULL AND
      (NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind
       OR NEW.execution_data IS NOT OLD.execution_data OR NEW.payload IS NOT OLD.payload
       OR NEW.capability_version_id IS NOT OLD.capability_version_id
       OR NEW.root_execution_id IS NOT OLD.root_execution_id
       OR NEW.parent_execution_id IS NOT OLD.parent_execution_id
       OR NEW.correlation_id IS NOT OLD.correlation_id OR NEW.child_key IS NOT OLD.child_key
       OR NEW.request_key IS NOT OLD.request_key OR NEW.request_fingerprint IS NOT OLD.request_fingerprint)
    BEGIN SELECT RAISE(ABORT, 'Execution version, lock and invocation are immutable'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS execution_state_transition
    BEFORE UPDATE OF state ON operation
    WHEN OLD.execution_data IS NOT NULL AND NEW.state != OLD.state AND NOT
      ({
        " OR ".join(
            f"(OLD.state = '{state.value}' AND NEW.state IN "
            f"({','.join(repr(target.value) for target in sorted(ExecutionStateMachine.targets(state)))}))"
            for state in ExecutionState
            if ExecutionStateMachine.targets(state)
        )
    })
    BEGIN SELECT RAISE(ABORT, 'Illegal execution state transition'); END""",
)

from shovel.db.model.runtime import Operation  # noqa: E402

for _statement in EXECUTION_DDL:
    _ddl = DDL(_statement)  # type: ignore[no-untyped-call]
    event.listen(Operation.__table__, "after_create", _ddl.execute_if(dialect="sqlite"))

KERNEL_INDEX_DDL = (
    """CREATE INDEX IF NOT EXISTS idx_execution_owner ON operation(
    json_extract(execution_data, '$.context.scope.workspace_id'),
    json_extract(execution_data, '$.context.scope.user_id'), state, queued_at, id)
    WHERE execution_data IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_execution_queue ON operation(queued_at, id)
    WHERE kind='capability.invoke' AND state='queued'""",
)

for _statement in KERNEL_INDEX_DDL:
    event.listen(
        Operation.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
