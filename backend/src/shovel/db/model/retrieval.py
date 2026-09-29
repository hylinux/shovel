"""Domain E/F — retrieval side and system tables."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from ...domain.enums import (
    AgentScopeMode,
    DeletionScope,
    FeedbackSignal,
    RetrievalRoute,
    Sensitivity,
)
from ..base import Base, Enum, JSONDict, JSONList, new_id, now_ts


class RetrievalLog(Base):
    """Every retrieval, with the route the planner chose.

    Three uses, all of which need the log to exist from day one:
    ranking signals, index blind-spot analysis (queries that keep scoring
    badly), and a regression set distilled from real usage.
    """

    __tablename__ = "retrieval_log"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("ret"))
    query_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    agent_run_id: Mapped[str | None] = mapped_column(sa.Text)
    route: Mapped[RetrievalRoute] = mapped_column(
        Enum(RetrievalRoute), default=RetrievalRoute.SEMANTIC, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(
        "filters_json", JSONDict, default=dict, nullable=False)
    returned: Mapped[list[dict[str, Any]]] = mapped_column(
        "returned_hits_json", JSONList, default=list, nullable=False)
    #: memory ids recalled alongside the KB hits -- lets you measure how often
    #: memory actually contributed to an answer
    memory_used: Mapped[list[str]] = mapped_column(
        "memory_used_json", JSONList, default=list, nullable=False)
    top_score: Mapped[float | None] = mapped_column(sa.Float)
    coverage_warning: Mapped[str | None] = mapped_column(sa.Text)
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    queried_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (
        Index("idx_rlog_ts", "queried_at"),
        Index("idx_rlog_weak", "top_score", sqlite_where=text("top_score < 0.5")),
        Index("idx_rlog_route", "route", "queried_at"),
    )


class RetrievalFeedback(Base):
    """Which results were actually used. Feeds ``interaction_score`` and the
    evaluation set; ``cited`` is the strongest signal available."""

    __tablename__ = "retrieval_feedback"

    retrieval_id: Mapped[str] = mapped_column(
        ForeignKey("retrieval_log.id", ondelete="CASCADE"), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    signal: Mapped[FeedbackSignal] = mapped_column(Enum(FeedbackSignal), primary_key=True)
    signalled_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (
        Index("idx_feedback_chunk", "chunk_id", "signal"),
        {"sqlite_with_rowid": False},
    )


class AgentScope(Base):
    """Per-agent authorisation over resources.

    This filter belongs INSIDE the Zvec query, never as a post-filter:
    filtering after the fact lets unauthorised content consume top_k and risks
    leaking it into reranking or logs.
    """

    __tablename__ = "agent_scope"

    agent_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True)
    mode: Mapped[AgentScopeMode] = mapped_column(
        Enum(AgentScopeMode), default=AgentScopeMode.READ, nullable=False)
    sensitivity_max: Mapped[Sensitivity] = mapped_column(
        Enum(Sensitivity), default=Sensitivity.NORMAL, nullable=False)
    #: memory is scoped separately: an agent may be allowed to read documents
    #: without being allowed to recall what the user said in private
    can_access_memory: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    granted_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = ({"sqlite_with_rowid": False},)


class EvalCase(Base):
    """Regression set. Populated mostly from thumbs-up / cited feedback, so it
    grows by itself and reflects what the user actually cares about."""

    __tablename__ = "eval_case"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("evc"))
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(
        "expected_json", JSONDict, default=dict, nullable=False)
    source: Mapped[str | None] = mapped_column(sa.Text)
    tags: Mapped[list[str]] = mapped_column(
        "tags_json", JSONList, default=list, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)


class DeletionOutbox(Base):
    """Deletion intent, committed to SQLite before being pushed to Zvec.

    There is no cross-store transaction. Committing intent first and pushing it
    idempotently is the simplest thing that cannot leave orphaned vectors; the
    reverse order silently can. Retrying a delete-by-filter is harmless.
    """

    __tablename__ = "deletion_outbox"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    scope: Mapped[DeletionScope] = mapped_column(Enum(DeletionScope), nullable=False)
    collection: Mapped[str | None] = mapped_column(sa.Text)   # NULL = all collections
    resource_id: Mapped[str | None] = mapped_column(sa.Text)
    doc_uri: Mapped[str | None] = mapped_column(sa.Text)
    keep_rev: Mapped[int | None] = mapped_column(sa.Integer)
    filter: Mapped[dict[str, Any]] = mapped_column(
        "filter_json", JSONDict, default=dict, nullable=False)
    requested_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    done_at: Mapped[int | None] = mapped_column(sa.Integer)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        Index("idx_outbox_todo", "requested_at", sqlite_where=text("done_at IS NULL")),
    )


class SchemaMigration(Base):
    __tablename__ = "schema_migration"

    version: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    applied_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)


class AppSetting(Base):
    """Runtime metadata: active model versions, GC timestamps, feature flags."""

    __tablename__ = "app_setting"

    setting_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    setting_value: Mapped[str | None] = mapped_column(sa.Text)
