"""Domain D — knowledge: entity, mention, edge, and the structured fact tables.

This is the graph, built inside SQLite. At personal-knowledge-base scale
(≤ a few million edges) recursive CTEs traverse it in milliseconds; a dedicated
graph database only starts paying for itself at ~5M edges, 4+ hops, or when
graph *algorithms* (PageRank, community detection) are needed. Until then a
third store would only add a third consistency problem and a second source of
truth.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import (
    DecisionState,
    EdgeSource,
    EntityKind,
    MentionRole,
    RelationKind,
    TaskState,
)
from ..base import Base, Enum, JSONList, new_id, now_ts


class Entity(Base):
    """A normalised person / project / org / topic.

    Entity resolution is the foundation of everything above it: the graph, the
    ``people`` filter in the vector payload, and the link between memory and the
    knowledge base all key off ``entity.id``. Noisy entities poison all three.
    """

    __tablename__ = "entity"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("ent"))
    kind: Mapped[EntityKind] = mapped_column(Enum(EntityKind), nullable=False)
    canonical: Mapped[str] = mapped_column(sa.Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        "aliases_json", JSONList, default=list, nullable=False)
    #: disambiguation key for people -- addresses are far more reliable than names
    emails: Mapped[list[str]] = mapped_column(
        "emails_json", JSONList, default=list, nullable=False)
    #: marks the user themselves; drives authority = 1.0 on their own content
    is_self: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    first_seen_at: Mapped[int | None] = mapped_column(sa.Integer)
    last_seen_at: Mapped[int | None] = mapped_column(sa.Integer)
    mention_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    is_user_confirmed: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, nullable=False)

    #: graph-algorithm results written back by an offline job (rustworkx in
    #: memory is enough at this scale -- no graph database required)
    centrality: Mapped[float | None] = mapped_column(sa.Float)
    community_id: Mapped[str | None] = mapped_column(sa.Text)

    mentions: Mapped[list[Mention]] = relationship(
        back_populates="entity", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("kind", "canonical", name="uq_entity_canonical"),
        Index("idx_entity_lookup", "kind", "canonical"),
        Index("idx_entity_self", "is_self", sqlite_where=sa.text("is_self = 1")),
    )


class Mention(Base):
    """Entity ↔ document bipartite edge. The cheapest, most reliable graph layer."""

    __tablename__ = "mention"

    entity_id: Mapped[str] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE"), primary_key=True)
    doc_uri: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    #: '' rather than NULL: NULL columns cannot participate in a primary key
    chunk_id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default="")
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    role: Mapped[MentionRole | None] = mapped_column(Enum(MentionRole))
    confidence: Mapped[float] = mapped_column(sa.Float, default=1.0, nullable=False)
    mentioned_at: Mapped[int | None] = mapped_column(sa.Integer)

    entity: Mapped[Entity] = relationship(back_populates="mentions")

    __table_args__ = (
        Index("idx_mention_timeline", "entity_id", "mentioned_at"),
        Index("idx_mention_doc", "doc_uri", "resource_id"),
        {"sqlite_with_rowid": False},
    )


class EntityEdge(Base):
    """A direct entity-to-entity relation — the layer that turns a bipartite
    index into a traversable graph.

    ``evidence`` is not optional. Auto-parsed relations are wrong often
    enough that a graph without provenance makes the agent *more* confident and
    *more* wrong. When the user asks "why do you think 张三 works on Shovel?",
    the answer must be a quote and a document, not a confidence score.
    """

    __tablename__ = "entity_edge"

    src_id: Mapped[str] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE"), primary_key=True)
    dst_id: Mapped[str] = mapped_column(
        ForeignKey("entity.id", ondelete="CASCADE"), primary_key=True)
    rel: Mapped[RelationKind] = mapped_column(Enum(RelationKind), primary_key=True)

    weight: Mapped[float] = mapped_column(sa.Float, default=1.0, nullable=False)
    source: Mapped[EdgeSource] = mapped_column(
        Enum(EdgeSource), default=EdgeSource.COOCCURRENCE, nullable=False)
    first_seen_at: Mapped[int | None] = mapped_column(sa.Integer)
    last_seen_at: Mapped[int | None] = mapped_column(sa.Integer)
    evidence_count: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    #: [{"doc_uri": ..., "chunk_id": ..., "quote": ...}]
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        "evidence_json", JSONList, default=list, nullable=False)
    extracted_by: Mapped[str | None] = mapped_column(sa.Text)   # model version
    is_user_confirmed: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("src_id <> dst_id", name="no_self_loop"),
        Index("idx_edge_out", "src_id", "rel", "weight"),
        Index("idx_edge_in", "dst_id", "rel", "weight"),
        {"sqlite_with_rowid": False},
    )


class FactDecision(Base):
    """A decision node with a supersession chain.

    This is what makes "how did this decision evolve?" answerable. No amount of
    vector similarity reconstructs an ordering; the ``supersedes`` edge does.
    """

    __tablename__ = "fact_decision"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("dec"))
    doc_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    chunk_id: Mapped[str | None] = mapped_column(sa.Text)
    statement: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    decided_at: Mapped[int | None] = mapped_column(sa.Integer)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL"))
    supersedes: Mapped[str | None] = mapped_column(sa.Text)
    decision_state: Mapped[DecisionState] = mapped_column(
        Enum(DecisionState), default=DecisionState.ACTIVE, nullable=False)

    __table_args__ = (
        Index("idx_decision_project", "project_id", "decided_at"),
        Index("idx_decision_chain", "supersedes"),
    )


class FactMessage(Base):
    """Email / chat headers.

    Headers are already structured, so they are queried with SQL and NEVER
    vectorised: ``WHERE from_entity = ? AND sent_at BETWEEN ? AND ?`` is exact,
    while the equivalent vector search recalls maybe 40%. The body still goes
    through chunking and embedding -- two channels, joined by ``doc_uri``.
    """

    __tablename__ = "fact_message"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("msg"))
    doc_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(sa.Text)
    from_entity: Mapped[str | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL"))
    to: Mapped[list[str]] = mapped_column("to_addresses_json", JSONList, default=list, nullable=False)
    cc: Mapped[list[str]] = mapped_column("cc_addresses_json", JSONList, default=list, nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.Text)
    sent_at: Mapped[int | None] = mapped_column(sa.Integer)
    folder: Mapped[str | None] = mapped_column(sa.Text)
    is_sent: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    has_attachment: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    attachment_uris: Mapped[list[str]] = mapped_column(
        "attachment_uris_json", JSONList, default=list, nullable=False)

    __table_args__ = (
        Index("idx_msg_from", "from_entity", "sent_at"),
        Index("idx_msg_thread", "thread_id", "sent_at"),
        Index("idx_msg_doc", "doc_uri", "resource_id"),
    )


class FactEvent(Base):
    """Calendar entries. Same rule: structured in, structured out."""

    __tablename__ = "fact_event"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("evt"))
    doc_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text)
    starts_at: Mapped[int | None] = mapped_column(sa.Integer)
    ends_at: Mapped[int | None] = mapped_column(sa.Integer)
    location: Mapped[str | None] = mapped_column(sa.Text)
    organizer: Mapped[str | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL"))
    attendees: Mapped[list[str]] = mapped_column(
        "attendees_json", JSONList, default=list, nullable=False)
    is_recurring: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    transcript_uri: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (Index("idx_event_range", "starts_at", "ends_at"),)


class FactTask(Base):
    """Commitments parsed from documents and conversations.

    Powers the constraint-style question no vector search can answer:
    "what did I promise but not deliver?"
    """

    __tablename__ = "fact_task"

    id: Mapped[str] = mapped_column(
        sa.Text, primary_key=True, default=lambda: new_id("tsk"))
    doc_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    chunk_id: Mapped[str | None] = mapped_column(sa.Text)
    text_: Mapped[str] = mapped_column("text", sa.Text, nullable=False)
    owner_entity: Mapped[str | None] = mapped_column(
        ForeignKey("entity.id", ondelete="SET NULL"))
    due_at: Mapped[int | None] = mapped_column(sa.Integer)
    task_state: Mapped[TaskState] = mapped_column(
        Enum(TaskState), default=TaskState.OPEN, nullable=False)
    extracted_by: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (Index("idx_task_due", "task_state", "due_at"),)
