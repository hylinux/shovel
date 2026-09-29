"""Domain C — content: document, chunk, document_derived, blob.

The ``chunk_fts`` FTS5 virtual table cannot be expressed in the ORM; it lives in
``db/ddl_extras.py`` together with the views.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import (
    BlobKind,
    DerivedKind,
    DocClass,
    DocumentLifecycleState,
    DocumentPipeState,
    ErrorKind,
    Granularity,
    Modality,
    VectorState,
)
from ..base import Base, Enum, JSONDict, JSONList, now_ts


class Document(Base):
    """A single addressable item at the source. Identity is ``(uri, resource_id)``.

    One wide table for every resource type -- never one table per type. Every
    high-frequency query (deletion propagation, failure statistics, the work
    queue) is cross-source, and per-type tables turn all of them into UNIONs.
    Type-specific fields live in ``source_meta``; only columns that appear in a
    WHERE clause are promoted to real columns.
    """

    __tablename__ = "document"

    uri: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True)

    # classification -- drives parser / chunker / embedder routing
    mime: Mapped[str | None] = mapped_column(sa.Text)
    doc_class: Mapped[DocClass] = mapped_column(
        Enum(DocClass), default=DocClass.UNKNOWN, nullable=False)
    modality: Mapped[Modality] = mapped_column(
        Enum(Modality), default=Modality.TEXT, nullable=False)

    # change detection
    content_hash: Mapped[str | None] = mapped_column(sa.Text)   # cheap fingerprint
    blob_hash: Mapped[str | None] = mapped_column(sa.Text)      # CAS key (true sha256)
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer)

    # lifecycle / deletion propagation
    lifecycle_state: Mapped[DocumentLifecycleState] = mapped_column(
        Enum(DocumentLifecycleState), default=DocumentLifecycleState.ACTIVE, nullable=False)
    #: job id of the last sweep that saw this document. "Not seen this run" ==
    #: ``last_seen_run != current_job_id`` -- the observed-set diff, as one UPDATE.
    last_seen_run: Mapped[str | None] = mapped_column(sa.Text)
    last_seen_at: Mapped[int | None] = mapped_column(sa.Integer)
    tombstoned_at: Mapped[int | None] = mapped_column(sa.Integer)
    skip_reason: Mapped[str | None] = mapped_column(sa.Text)

    # pipeline state (resumable)
    pipe_state: Mapped[DocumentPipeState] = mapped_column(
        Enum(DocumentPipeState), default=DocumentPipeState.PENDING, nullable=False)
    pipe_error_kind: Mapped[ErrorKind | None] = mapped_column(Enum(ErrorKind))
    pipe_attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    #: hash that is currently indexed; != content_hash means "re-run me"
    indexed_hash: Mapped[str | None] = mapped_column(sa.Text)
    chunker_version: Mapped[str | None] = mapped_column(sa.Text)
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    rev: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)

    # common queryable metadata
    title: Mapped[str | None] = mapped_column(sa.Text)
    lang: Mapped[str | None] = mapped_column(sa.Text)
    author_entity_id: Mapped[str | None] = mapped_column(sa.Text)
    created_at_src: Mapped[int | None] = mapped_column(sa.Integer)
    modified_at_src: Mapped[int | None] = mapped_column(sa.Integer)
    ingested_at: Mapped[int | None] = mapped_column(sa.Integer)

    # ranking signals, computed at write time and mirrored into the Zvec fields
    authority: Mapped[float] = mapped_column(sa.Float, default=0.6, nullable=False)
    interaction_score: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    is_canonical: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    dup_cluster_id: Mapped[str | None] = mapped_column(sa.Text)

    source_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONDict, default=dict, nullable=False)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_doc_sweep", "resource_id", "lifecycle_state", "last_seen_run"),
        Index("idx_doc_pending", "resource_id", "pipe_state",
              sqlite_where=text("pipe_state IN ('pending','failed')")),
        Index("idx_doc_tomb", "tombstoned_at",
              sqlite_where=text("lifecycle_state = 'tombstoned'")),
        Index("idx_doc_blob", "blob_hash"),
        Index("idx_doc_recent", "resource_id", "modified_at_src",
              sqlite_where=text("lifecycle_state = 'active'")),
        Index("idx_doc_author", "author_entity_id"),
        # expression index: email threading without promoting thread_id to a column
        Index("idx_doc_thread", text("json_extract(source_meta, '$.thread_id')"),
              sqlite_where=text("doc_class = 'email'")),
    )

    @property
    def needs_processing(self) -> bool:
        if self.lifecycle_state != DocumentLifecycleState.ACTIVE:
            return False
        if self.pipe_state in (DocumentPipeState.PENDING, DocumentPipeState.FAILED):
            return True
        return self.indexed_hash != self.content_hash


class Chunk(Base):
    """The retrieval unit AND the reading unit.

    ``granularity`` lets one table carry every retrieval layer, so
    ``expand_chunk``, deletion propagation and dedup are each written once:

    * ``chunk``  -- small, embedded, searched (high precision)
    * ``parent`` -- large, NOT embedded, what ``expand_chunk`` returns (readable)
    * ``section`` / ``doc_summary`` -- coarse layers for overview questions
    * ``hypothetical_question`` -- query-shaped text pointing at its answer chunk

    Per-layer behaviour (embed / returnable / producing stage) is declared on
    :class:`~shovel.domain.enums.Granularity`, never re-derived here.
    """

    __tablename__ = "chunk"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)  # == the Zvec doc id
    doc_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(sa.Text, nullable=False)

    granularity: Mapped[Granularity] = mapped_column(
        Enum(Granularity), default=Granularity.CHUNK, nullable=False)
    chunk_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    parent_chunk_id: Mapped[str | None] = mapped_column(sa.Text)   # small-to-big
    answers_chunk_id: Mapped[str | None] = mapped_column(sa.Text)
    covers: Mapped[list[str]] = mapped_column(
        "covered_chunk_ids_json", JSONList, default=list, nullable=False)

    #: AUTHORITATIVE copy of the text. Zvec also holds a copy in its own `text`
    #: field to power native full-text search, but this one is the source of
    #: truth and the one `expand_chunk` reads: expansion is a range query
    #: (``chunk_index BETWEEN a AND b``), which SQLite answers in one statement
    #: and a vector store can only answer by fetching documents one id at a time.
    text_: Mapped[str] = mapped_column("text", sa.Text, nullable=False)
    context_prefix: Mapped[str | None] = mapped_column(sa.Text)
    token_count: Mapped[int | None] = mapped_column(sa.Integer)
    rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)

    vector_state: Mapped[VectorState] = mapped_column(
        Enum(VectorState), default=VectorState.PENDING, nullable=False)
    vector_collection: Mapped[str | None] = mapped_column(sa.Text)
    model_version: Mapped[str | None] = mapped_column(sa.Text)

    simhash: Mapped[int | None] = mapped_column(sa.BigInteger)
    is_canonical: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)

    chunk_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONDict, default=dict, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        ForeignKeyConstraint(
            ["doc_uri", "resource_id"], ["document.uri", "document.resource_id"],
            ondelete="CASCADE", name="fk_chunk_document"),
        # expand_chunk hot path -- asserted with EXPLAIN QUERY PLAN in the tests
        Index("idx_chunk_expand", "doc_uri", "resource_id", "chunk_index"),
        Index("idx_chunk_parent", "parent_chunk_id",
              sqlite_where=text("parent_chunk_id IS NOT NULL")),
        Index("idx_chunk_pending", "vector_state",
              sqlite_where=text("vector_state = 'pending'")),
        Index("idx_chunk_simhash", "simhash", sqlite_where=text("simhash IS NOT NULL")),
        Index("idx_chunk_gran", "resource_id", "granularity"),
    )

    @property
    def embed_text(self) -> str:
        """What actually goes to the embedder. The provenance prefix lifts recall
        more than swapping embedding models does."""
        return f"{self.context_prefix}\n\n{self.text_}" if self.context_prefix else self.text_


class DocumentDerived(Base):
    """Document-level derived artefacts: summary, outline, keywords.

    Produced by a low-priority stage: a missing summary must never block
    retrieval, so this is deliberately a separate table, not columns on Document.
    """

    __tablename__ = "document_derived"

    doc_uri: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    resource_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    kind: Mapped[DerivedKind] = mapped_column(Enum(DerivedKind), primary_key=True)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(sa.Text)
    generated_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["doc_uri", "resource_id"], ["document.uri", "document.resource_id"],
            ondelete="CASCADE", name="fk_document_derived_document"),
    )


class BlobRef(Base):
    """Content-addressed store index.

    The same attachment appearing in mail, on disk and in a chat is processed
    once. It also means a model upgrade can re-embed without going back to
    sources that may no longer be reachable.
    """

    __tablename__ = "blob_ref"

    hash: Mapped[str] = mapped_column(sa.Text, primary_key=True)   # sha256
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)     # blobs/ab/cd/abcd...
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer)
    mime: Mapped[str | None] = mapped_column(sa.Text)
    kind: Mapped[BlobKind] = mapped_column(
        Enum(BlobKind), default=BlobKind.RAW, nullable=False)
    ref_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    last_used_at: Mapped[int | None] = mapped_column(sa.Integer)

    __table_args__ = (
        Index("idx_blob_gc", "last_used_at", sqlite_where=text("ref_count = 0")),
    )
