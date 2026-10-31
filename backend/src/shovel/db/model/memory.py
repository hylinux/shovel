"""Authoritative native memory records. Vector indexes contain derived data only."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import (
    MemoryKind,
    MemoryLinkKind,
    MemoryPredicate,
    MemorySource,
    MemoryValidityState,
    VectorState,
    WorkingSlot,
)
from ..base import (
    Base,
    Enum,
    JSONDict,
    JSONList,
    SalienceMixin,
    ValidityMixin,
    new_id,
    now_ts,
)


# --------------------------------------------------------------------------- #
# 1. WORKING MEMORY — scoped to one task
# --------------------------------------------------------------------------- #
class MemoryWorking(Base):
    """Scratch state for one agent run.

    Working memory *will* overflow: a 50-turn task cannot keep every tool result
    in context. Rather than silently truncating (which drops the goal along with
    the noise), slots are typed and compacted by priority — ``scratch`` first,
    ``goal`` never. ``archived_blob_hash`` keeps the full text in the CAS so a
    compacted slot can still be reopened by id.
    """

    __tablename__ = "memory_working"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("wm"))
    session_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    turn: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    slot: Mapped[WorkingSlot] = mapped_column(Enum(WorkingSlot), default=WorkingSlot.SCRATCH, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(sa.Integer)

    validity_state: Mapped[MemoryValidityState] = mapped_column(
        Enum(MemoryValidityState), default=MemoryValidityState.ACTIVE, nullable=False
    )
    #: set when this slot was compacted away; the summary that replaced it
    compacted_into: Mapped[str | None] = mapped_column(sa.Text)
    archived_blob_hash: Mapped[str | None] = mapped_column(sa.Text)

    #: ids of evidence this slot refers to, so a compacted slot still cites
    refs: Mapped[list[str]] = mapped_column("ref_ids_json", JSONList, default=list, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("meta_json", JSONDict, default=dict, nullable=False)

    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    expires_at: Mapped[int | None] = mapped_column(sa.Integer)

    __table_args__ = (
        Index("idx_wm_session", "session_id", "turn"),
        Index("idx_wm_live", "session_id", "slot", sqlite_where=text("validity_state = 'active'")),
        Index("idx_wm_gc", "expires_at", sqlite_where=text("expires_at IS NOT NULL")),
    )

    @property
    def compactable(self) -> bool:
        """Goals, plans and errors survive compaction.

        Errors are kept deliberately: an agent that forgets it already tried
        something will try it again, forever.
        """
        return self.slot not in (WorkingSlot.GOAL, WorkingSlot.PLAN, WorkingSlot.ERROR)


class MemoryOwnership:
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(sa.Text, default="agent", nullable=False)
    fingerprint: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    provenance: Mapped[list[dict[str, str | int | None]]] = mapped_column(
        JSONList, default=list, nullable=False
    )


class NativeMemory(Base, ValidityMixin, SalienceMixin, MemoryOwnership):
    __abstract__ = True

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    session_id: Mapped[str | None] = mapped_column(sa.Text)
    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False
    )
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    validity_state: Mapped[MemoryValidityState] = mapped_column(
        Enum(MemoryValidityState), default=MemoryValidityState.ACTIVE, nullable=False
    )


# --------------------------------------------------------------------------- #
# 2. EPISODIC MEMORY — what happened
# --------------------------------------------------------------------------- #
class MemoryEpisode(NativeMemory):
    """A recorded episode: a conversation, a decision, a piece of work.

    Vectorised into the ``shovel_memory`` collection so it can be recalled
    semantically ("what did we discuss about the graph database?"). Decays via
    :class:`SalienceMixin` rather than deletion — an unused episode sinks in the
    ranking but stays answerable.
    """

    __tablename__ = "memory_episode"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("epi"))
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    session_id: Mapped[str | None] = mapped_column(sa.Text)

    #: one-line gist; this is what gets embedded and injected
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.Text)
    outcome: Mapped[str | None] = mapped_column(sa.Text)  # what was concluded/produced

    occurred_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    source: Mapped[MemorySource] = mapped_column(
        Enum(MemorySource), default=MemorySource.STATED, nullable=False
    )
    validity_state: Mapped[MemoryValidityState] = mapped_column(
        Enum(MemoryValidityState), default=MemoryValidityState.ACTIVE, nullable=False
    )

    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False
    )
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)

    tags: Mapped[list[str]] = mapped_column("tags_json", JSONList, default=list, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("meta_json", JSONDict, default=dict, nullable=False)

    links: Mapped[list[MemoryLink]] = relationship(
        primaryjoin="and_(MemoryLink.memory_id == foreign(MemoryEpisode.id), "
        "MemoryLink.memory_kind == 'episodic')",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_epi_time", "occurred_at"),
        Index(
            "idx_epi_active", "validity_state", "occurred_at", sqlite_where=text("validity_state = 'active'")
        ),
        Index("idx_epi_pending", "vector_state", sqlite_where=text("vector_state = 'pending'")),
        Index("idx_epi_session", "session_id"),
    )


# --------------------------------------------------------------------------- #
# 3. SEMANTIC MEMORY — what the user is like
# --------------------------------------------------------------------------- #
class MemoryBelief(NativeMemory):
    """A (subject, predicate, object) belief about the user or an entity.

    The triple shape is what makes contradiction *detectable*: two facts
    conflict when they share ``(subject, predicate)`` and the predicate is
    single-valued (see :meth:`MemoryPredicate.single_valued`). A free-text
    "note to self" field would make conflict detection undecidable, which is
    why this table is deliberately not free text.

    Supersession never deletes. "We previously chose SQLite over Kùzu" must stay
    answerable after the choice is reversed — that history is often the most
    valuable thing in the store.
    """

    __tablename__ = "memory_belief"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("fact"))
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    session_id: Mapped[str | None] = mapped_column(sa.Text)
    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False
    )
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    #: 'user' or an entity.id — memories may be about other people too
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False, default="user")
    predicate: Mapped[MemoryPredicate] = mapped_column(Enum(MemoryPredicate), nullable=False)
    object_: Mapped[str] = mapped_column("object", sa.Text, nullable=False)
    #: optional narrowing of the object's domain, so "prefers" can hold several
    #: non-conflicting values: (prefers, deployment)=local, (prefers, language)=python
    object_domain: Mapped[str | None] = mapped_column(sa.Text)

    source: Mapped[MemorySource] = mapped_column(
        Enum(MemorySource), default=MemorySource.INFERRED, nullable=False
    )
    validity_state: Mapped[MemoryValidityState] = mapped_column(
        Enum(MemoryValidityState), default=MemoryValidityState.ACTIVE, nullable=False
    )

    #: [{"session_id": ..., "quote": ..., "ts": ...}] — why we believe this
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        "evidence_json", JSONList, default=list, nullable=False
    )
    #: free-text elaboration shown to the user, never used for matching
    note: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        # At most ONE active row per (subject, predicate, object, domain).
        # A partial unique index rather than a plain one: superseded rows stay in
        # the table and must be allowed to duplicate the active row's key.
        Index(
            "uq_belief_active",
            "workspace_id",
            "user_id",
            "scope_kind",
            "agent_id",
            "session_id",
            "subject",
            "predicate",
            "object",
            "object_domain",
            unique=True,
            sqlite_where=text("validity_state = 'active'"),
        ),
        Index("idx_belief_subject", "subject", "predicate", sqlite_where=text("validity_state = 'active'")),
        Index("idx_belief_validity", "validity_state", "valid_until"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="conf_range"),
    )

    @property
    def is_currently_valid(self) -> bool:
        """Status and world-time validity are independent; both must hold."""
        if self.validity_state != MemoryValidityState.ACTIVE:
            return False
        now = now_ts()
        if self.valid_from and now < self.valid_from:
            return False
        return not (self.valid_until and now >= self.valid_until)

    def render(self) -> str:
        """One line for the system prompt."""
        dom = f"[{self.object_domain}] " if self.object_domain else ""
        return f"{self.subject} {self.predicate} {dom}{self.object_}"


# --------------------------------------------------------------------------- #
# 4. PROCEDURAL MEMORY — how to do things for this user
# --------------------------------------------------------------------------- #
class MemoryProcedure(NativeMemory):
    """A learned behavioural rule: "when X, do Y".

    v1 should populate this ONLY from explicit user corrections. Auto-inferred
    behaviour rules are the fastest way to build an agent that is confidently
    wrong in a way the user cannot see or undo — so ``source`` is recorded and
    inferred rules stay advisory until confirmed.
    """

    __tablename__ = "memory_procedure"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("proc"))
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    session_id: Mapped[str | None] = mapped_column(sa.Text)
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: when this applies — natural language, matched semantically
    trigger_pattern: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Advisory workflow text, subject to the same untrusted context budget.
    instruction: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: optional narrowing: only for this resource / doc_class / tool
    scope: Mapped[dict[str, Any]] = mapped_column("scope_json", JSONDict, default=dict, nullable=False)

    source: Mapped[MemorySource] = mapped_column(
        Enum(MemorySource), default=MemorySource.CORRECTED, nullable=False
    )
    validity_state: Mapped[MemoryValidityState] = mapped_column(
        Enum(MemoryValidityState), default=MemoryValidityState.ACTIVE, nullable=False
    )

    # outcome tracking — a rule that keeps preceding thumbs-down should decay
    applied_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        "evidence_json", JSONList, default=list, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_procedure_name",
            "workspace_id",
            "user_id",
            "scope_kind",
            "agent_id",
            "session_id",
            "name",
            unique=True,
            sqlite_where=text("validity_state = 'active'"),
        ),
        Index("idx_proc_active", "validity_state", sqlite_where=text("validity_state = 'active'")),
    )

    @property
    def success_rate(self) -> float | None:
        total = self.success_count + self.failure_count
        return None if total == 0 else self.success_count / total


class SessionSummary(Base):
    __tablename__ = "session_summary"

    conversation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    through_sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)


class ChatToolInteraction(Base):
    """Audit metadata only; tool payloads never become implicit user facts."""

    __tablename__ = "chat_tool_interaction"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("tool"))
    conversation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(sa.Text)
    tool_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    result_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)


class MemoryIndexOutbox(Base):
    __tablename__ = "memory_index_outbox"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("midx"))
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    memory_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    retry_at: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    done_at: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)


class MemoryIndexState(Base):
    __tablename__ = "memory_index_state"

    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    generation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    building_generation: Mapped[str | None] = mapped_column(sa.Text)
    building_model_version: Mapped[str | None] = mapped_column(sa.Text)
    state: Mapped[str] = mapped_column(sa.Text, default="pending", nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)


# --------------------------------------------------------------------------- #
# 5. LINKS — the seam between memory and the knowledge base
# --------------------------------------------------------------------------- #
class MemoryLink(Base):
    """Attaches any memory to an entity / document / chunk / decision.

    One polymorphic table rather than four link tables, because every consumer
    query is "give me all memories touching X" regardless of memory kind, and
    four tables would make that a UNION.

    The entity link is the important one: it is what allows a single answer to
    draw on both "what we said" (memory) and "what the documents say" (KB).
    """

    __tablename__ = "memory_link"

    memory_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    memory_kind: Mapped[MemoryKind] = mapped_column(Enum(MemoryKind), primary_key=True)
    target_kind: Mapped[MemoryLinkKind] = mapped_column(Enum(MemoryLinkKind), primary_key=True)
    #: entity.id / document.uri / chunk.id / fact_decision.id / resource.id
    target_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    #: needed only when target_kind is document/chunk (their keys are composite)
    target_resource_id: Mapped[str | None] = mapped_column(sa.Text)

    role: Mapped[str | None] = mapped_column(sa.Text)  # about|mentioned|derived_from
    weight: Mapped[float] = mapped_column(sa.Float, default=1.0, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (
        # the hot path: "memories about entity X"
        Index("idx_mlink_target", "target_kind", "target_id"),
        Index("idx_mlink_memory", "memory_kind", "memory_id"),
        {"sqlite_with_rowid": False},
    )


# --------------------------------------------------------------------------- #
# 6. REVISION LOG — an auditable trail of belief changes
# --------------------------------------------------------------------------- #
class MemoryRevision(Base):
    """Why a memory changed state.

    Lets the agent answer "when did you stop believing X, and why?" — which is
    what makes an agent that changes its mind trustworthy rather than erratic.
    """

    __tablename__ = "memory_revision"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    memory_kind: Mapped[MemoryKind] = mapped_column(Enum(MemoryKind), nullable=False)
    from_state: Mapped[MemoryValidityState | None] = mapped_column(Enum(MemoryValidityState))
    to_state: Mapped[MemoryValidityState] = mapped_column(Enum(MemoryValidityState), nullable=False)
    caused_by: Mapped[str | None] = mapped_column(sa.Text)  # winning memory id
    reason: Mapped[str | None] = mapped_column(sa.Text)
    actor: Mapped[str] = mapped_column(sa.Text, default="system", nullable=False)
    revised_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (Index("idx_mrev_memory", "memory_id", "revised_at"),)


# --------------------------------------------------------------------------- #
# 7. STANDING QUERY — the push channel (access path #7)
# --------------------------------------------------------------------------- #
class StandingQuery(Base):
    """A subscription: "tell me when something matching this arrives".

    Newly indexed chunks are matched against the (small) set of standing
    queries. This is what turns a question-answering box into an assistant that
    speaks first — and it reuses the existing trigger/job machinery.
    """

    __tablename__ = "standing_query"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("sq"))
    agent_id: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column("filters_json", JSONDict, default=dict, nullable=False)
    threshold: Mapped[float] = mapped_column(sa.Float, default=0.75, nullable=False)
    action: Mapped[str] = mapped_column(sa.Text, default="notify", nullable=False)

    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False
    )
    last_fired_at: Mapped[int | None] = mapped_column(sa.Integer)
    fire_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    #: silence window so one noisy import does not fire fifty notifications
    cooldown_seconds: Mapped[int] = mapped_column(sa.Integer, default=3600, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (Index("idx_sq_enabled", "is_enabled", sqlite_where=text("is_enabled = 1")),)
