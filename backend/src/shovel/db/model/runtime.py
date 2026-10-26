"""Durable commands and single-owner Agent conversations."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict, JSONList, new_id, now_ts


class Operation(Base):
    __tablename__ = "operation"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("op"))
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, default="queued", nullable=False, index=True)
    # Deliberately not cascading FKs: deletion must not erase its own receipt.
    resource_id: Mapped[str | None] = mapped_column(sa.Text)
    job_id: Mapped[str | None] = mapped_column(sa.Text, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text)
    queued_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    started_at: Mapped[int | None] = mapped_column(sa.Integer)
    finished_at: Mapped[int | None] = mapped_column(sa.Integer)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("chat"))
    title: Mapped[str] = mapped_column(sa.Text, default="新对话", nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    updated_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False, index=True)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(sa.Text, default="completed", nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONList, default=list, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSONList, default=list, nullable=False)
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    __table_args__ = (sa.UniqueConstraint("conversation_id", "sequence", name="uq_chat_sequence"),)
