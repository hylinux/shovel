"""Domain B — execution: job, job_event."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import ErrorKind, JobState, LogLevel, ScanMode, ScheduleKind, Stage
from ..base import Base, Enum, JSONDict, new_id, now_ts


class Job(Base):
    """One concrete execution. ``job.id`` doubles as the sweep run id that
    ``Document.last_seen_run`` points at -- that is what makes the observed-set
    diff a single UPDATE instead of an in-memory set difference."""

    __tablename__ = "job"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("job"))
    scan_profile_id: Mapped[str] = mapped_column(
        ForeignKey("scan_profile.id", ondelete="CASCADE"), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), nullable=False)
    schedule_id: Mapped[str | None] = mapped_column(sa.Text)   # NULL for manual runs
    fired_by_kind: Mapped[ScheduleKind] = mapped_column(Enum(ScheduleKind), nullable=False)
    scan_mode: Mapped[ScanMode] = mapped_column(Enum(ScanMode), nullable=False)

    state: Mapped[JobState] = mapped_column(
        Enum(JobState), default=JobState.QUEUED, nullable=False)
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
    is_discovery_complete: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, nullable=False)
    is_cancel_requested: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, nullable=False)
    cursor_out: Mapped[str | None] = mapped_column(sa.Text)

    error_kind: Mapped[ErrorKind | None] = mapped_column(Enum(ErrorKind))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    stats: Mapped[dict[str, Any]] = mapped_column(
        "stats_json", JSONDict, default=dict, nullable=False)

    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_job_queue", "priority", "queued_at",
              sqlite_where=text("state = 'queued'")),
        # "is a sibling job of the same kind already running?" -- prevents the
        # slow-scan + fast-cadence task avalanche
        Index("idx_job_active", "resource_id", "scan_mode",
              sqlite_where=text("state IN ('queued','running')")),
        Index("idx_job_zombie", "heartbeat_at", sqlite_where=text("state = 'running'")),
        Index("idx_job_hist", "resource_id", "queued_at"),
    )

    @property
    def progress(self) -> float:
        """0.0-1.0. Meaningless until discovery completes -- which is exactly why
        discovery is a separate, up-front stage."""
        if not self.is_discovery_complete or self.discovered == 0:
            return 0.0
        done = self.embedded + self.skipped_unchanged + self.failed_docs
        return min(1.0, done / self.discovered)

    @property
    def may_reconcile_deletions(self) -> bool:
        return self.scan_mode == ScanMode.FULL_SWEEP and self.is_discovery_complete

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
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job.id", ondelete="CASCADE"), nullable=False)
    logged_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    level: Mapped[LogLevel] = mapped_column(
        Enum(LogLevel), default=LogLevel.INFO, nullable=False)
    stage: Mapped[Stage | None] = mapped_column(Enum(Stage))
    doc_uri: Mapped[str | None] = mapped_column(sa.Text)
    code: Mapped[str | None] = mapped_column(sa.Text)   # PARSE_ENCRYPTED_PDF, ...
    message: Mapped[str | None] = mapped_column(sa.Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        "detail_json", JSONDict, default=dict, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_jobevent", "job_id", "logged_at"),
        Index("idx_jobevent_err", "job_id", "code",
              sqlite_where=text("level = 'error'")),
    )
