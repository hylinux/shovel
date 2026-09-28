"""Domain A — configuration: resource, scan_profile, trigger."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import MisfirePolicy, ResourceState, ScanMode, ScheduleKind, Sensitivity
from ..base import Base, Enum, JSONDict, JSONList, TimestampMixin, new_id, now_ts


class Resource(Base, TimestampMixin):
    """A user-created data-source instance.

    ``connector_kind`` is a key into the in-code connector registry and is
    deliberately NOT a foreign key: connectors are code, not user data. The rule is
    "configuration goes in a table, capability goes in a registry".
    """

    __tablename__ = "resource"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("res"))
    name: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    connector_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: key into the OS keyring -- never a plaintext secret
    identity_ref: Mapped[str | None] = mapped_column(sa.Text)
    config: Mapped[dict[str, Any]] = mapped_column(
        "config_json", JSONDict, default=dict, nullable=False)

    state: Mapped[ResourceState] = mapped_column(
        Enum(ResourceState), default=ResourceState.DRAFT, nullable=False)
    state_reason: Mapped[str | None] = mapped_column(sa.Text)
    last_verified_at: Mapped[int | None] = mapped_column(sa.Integer)

    sensitivity: Mapped[Sensitivity] = mapped_column(
        Enum(Sensitivity), default=Sensitivity.NORMAL, nullable=False)
    #: default ranking weight for documents from this source
    authority: Mapped[float] = mapped_column(sa.Float, default=0.6, nullable=False)
    #: one-liner shown to the agent in the catalog prompt
    description: Mapped[str | None] = mapped_column(sa.Text)

    profiles: Mapped[list[ScanProfile]] = relationship(
        back_populates="resource", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("authority >= 0.0 AND authority <= 1.0",
                        name="authority_range"),
        Index("idx_resource_state", "state"),
    )

    @property
    def is_scannable(self) -> bool:
        return self.state in (ResourceState.ACTIVE, ResourceState.DEGRADED)


class ScanProfile(Base, TimestampMixin):
    """What to scan, in which mode, with which pipeline overrides.

    Triggers hang off the profile rather than the resource, because one resource
    genuinely needs several cadences (10-minute incremental, weekly full sweep,
    hourly health check). Putting them on the resource forces an ever-growing
    list of ``*_interval`` columns.
    """

    __tablename__ = "scan_profile"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("prof"))
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scan_mode: Mapped[ScanMode] = mapped_column(Enum(ScanMode), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)

    include: Mapped[list[str]] = mapped_column(
        "include_globs_json", JSONList, default=list, nullable=False)
    exclude: Mapped[list[str]] = mapped_column(
        "exclude_globs_json", JSONList, default=list, nullable=False)
    max_file_bytes: Mapped[int] = mapped_column(
        sa.Integer, default=200 * 1024 * 1024, nullable=False)
    time_window_days: Mapped[int | None] = mapped_column(sa.Integer)

    #: {"chunker": {...}, "enable_stages": [...], "embedder": "..."}
    pipeline: Mapped[dict[str, Any]] = mapped_column(
        "pipeline_json", JSONDict, default=dict, nullable=False)

    # politeness budget -- this runs on the user's working machine
    max_concurrency: Mapped[int] = mapped_column(sa.Integer, default=2, nullable=False)
    cpu_budget_percent: Mapped[int] = mapped_column(sa.Integer, default=50, nullable=False)
    requires_ac_power: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    requires_idle: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    resource: Mapped[Resource] = relationship(back_populates="profiles")
    schedules: Mapped[list[Schedule]] = relationship(
        back_populates="profile", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("resource_id", "name", name="uq_profile_name"),
        CheckConstraint("cpu_budget_percent BETWEEN 1 AND 100", name="cpu_budget"),
        CheckConstraint("max_concurrency BETWEEN 1 AND 64", name="concurrency"),
        Index("idx_profile_resource", "resource_id", "is_enabled"),
    )

    @property
    def may_propagate_deletions(self) -> bool:
        """Only a full sweep can observe that something disappeared."""
        return self.scan_mode == ScanMode.FULL_SWEEP


class Schedule(Base):
    """When to run a scan profile.

    ``next_run_at`` is the scheduling watermark. The scheduler is a DB-driven
    due-poller, not an in-memory timer: on a laptop the process gets killed, the
    machine sleeps, and several processes may race. Persisting the watermark
    collapses all three problems into "SELECT the due rows after restart".
    """

    __tablename__ = "schedule"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("trg"))
    scan_profile_id: Mapped[str] = mapped_column(
        ForeignKey("scan_profile.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[ScheduleKind] = mapped_column(Enum(ScheduleKind), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(sa.Text)
    interval_seconds: Mapped[int | None] = mapped_column(sa.Integer)
    timezone: Mapped[str] = mapped_column(
        sa.Text, default="Asia/Shanghai", nullable=False)

    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(sa.Text)

    next_run_at: Mapped[int | None] = mapped_column(sa.Integer)
    last_run_at: Mapped[int | None] = mapped_column(sa.Integer)
    last_job_id: Mapped[str | None] = mapped_column(sa.Text)
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, default=0, nullable=False)

    misfire_policy: Mapped[MisfirePolicy] = mapped_column(
        Enum(MisfirePolicy), default=MisfirePolicy.COALESCE, nullable=False)
    jitter_seconds: Mapped[int] = mapped_column(sa.Integer, default=60, nullable=False)
    debounce_seconds: Mapped[int] = mapped_column(sa.Integer, default=30, nullable=False)

    # cross-process mutual exclusion via optimistic lease
    lease_owner: Mapped[str | None] = mapped_column(sa.Text)
    lease_until: Mapped[int | None] = mapped_column(sa.Integer)

    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    profile: Mapped[ScanProfile] = relationship(back_populates="schedules")

    __table_args__ = (
        CheckConstraint("kind <> 'cron' OR cron_expr IS NOT NULL", name="cron_expr_req"),
        CheckConstraint("kind <> 'interval' OR interval_seconds IS NOT NULL",
                        name="interval_req"),
        # guardrail: a user typing "10" must not mean a scan every 10 seconds
        CheckConstraint("interval_seconds IS NULL OR interval_seconds >= 300",
                        name="min_interval"),
        # the scheduler's hot path
        Index("idx_schedule_due", "next_run_at",
              sqlite_where=text("is_enabled = 1 AND next_run_at IS NOT NULL")),
        Index("idx_schedule_profile", "scan_profile_id"),
    )

    def is_time_driven(self) -> bool:
        return self.kind in (ScheduleKind.CRON, ScheduleKind.INTERVAL)
