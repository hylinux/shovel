from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, ClassVar

import sqlalchemy as sa
from sqlalchemy import Dialect, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "idx_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}

# Shovel 自己的命名空间
SHOVEL_NAMESPACE = uuid.UUID("92ae74ec-5e1f-52c8-b366-aae00b4006e1")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        pk = sa.inspect(self).identity
        return f"<{type(self).__name__} {pk}>"



## 帮助函数
def now_ts() -> int:
    """
    当前的unix epoch 秒 (UTC)
    """
    return int(time.time())


def new_id(prefix: str) -> str:

    return f"{prefix}_{uuid.uuid4().hex}"



# Chunk Id

def chunk_vector_id(doc_uri: str, content_hash: str, chunker_version: str,
                    granularity: str, index: int) -> str:
    """Deterministic id for one knowledge-base vector.

    This value IS ``chunk.id`` in SQLite AND the ``Doc.id`` in Zvec -- one key,
    two stores. That is what lets the two be joined, reconciled and deleted
    together without a mapping table.

    Determinism buys idempotency: re-running the pipeline over unchanged content
    regenerates identical ids, so ``upsert`` overwrites instead of duplicating.
    With random ids, a retried job silently doubles every vector and leaves you
    no way to tell the copies apart.

    Every component of the key defends against one specific collision:

    ``doc_uri``          two files with identical content must stay distinct
    ``content_hash``     an edited file must invalidate its old vectors
    ``chunker_version``  a new splitting strategy retires the old chunks
    ``granularity``      a parent block and its first child share everything
                         else -- without this they would overwrite each other
    ``index``            otherwise every chunk of a document collapses into one

    ``uuid5`` rather than a raw digest: vector stores expect UUID-shaped ids,
    and the namespace keeps our ids from colliding with another tool that
    happens to hash the same string.
    """
    key = f"{doc_uri}|{content_hash}|{chunker_version}|{granularity}|{index}"
    return str(uuid.uuid5(SHOVEL_NAMESPACE, key))



# Memory Vector Id

def memory_vector_id(memory_kind: str, memory_id: str, rev: int = 1) -> str:
    """Deterministic id for one memory vector.

    Separate from :func:`chunk_vector_id` because memories invalidate on a
    different trigger: a chunk gets a new id when its source CONTENT changes,
    a memory when it is REVISED (``rev`` increments). A memory also has no
    ``doc_uri`` -- it belongs to no document -- and no ``index``, being one
    vector rather than one of many.

    Forcing both through one function would mean passing dummy values for two
    parameters, and dummy values are where collisions come from.

    The ``mem|`` prefix guarantees the two key spaces can never produce the
    same string even by accident.
    """
    return str(uuid.uuid5(SHOVEL_NAMESPACE, f"mem|{memory_kind}|{memory_id}|{rev}"))





# --------------------------------------------------------------------------- #
# custom types
# --------------------------------------------------------------------------- #


class JSONEncoded(TypeDecorator):
    empty_factory: ClassVar[Callable[[], Any] | None] = None

    def empty_value(self) -> Any:
        factory = type(self).empty_factory
        return factory() if factory is not None else None

    def process_result_value(self, value, dialect):
        if not value:
            return self.empty_value()
        return json.loads(value)

class JSONList(JSONEncoded):
    empty_factory = list        # 工厂不是 []







def Enum(enum_cls, **kw) -> sa.Enum:
    """Non-native enum -> ``VARCHAR + CHECK (col IN (...))`` on SQLite."""
    return sa.Enum(
        enum_cls,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        name=kw.pop("name", None) or f"ck_{enum_cls.__name__.lower()}",
        **kw,
    )




# --------------------------------------------------------------------------- #
# mixins
# --------------------------------------------------------------------------- #
class TimestampMixin:
    """``created_at`` / ``updated_at`` as epoch seconds."""

    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    updated_at: Mapped[int] = mapped_column(
        sa.Integer, default=now_ts, onupdate=now_ts, nullable=False
    )


class CreatedAtMixin:
    created_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)



class ValidityMixin:
    """Bitemporal-lite validity, shared by every long-lived memory table.

    Two independent time axes:

    * **world time** -- ``valid_from`` / ``valid_until``: when the fact is true
      *in the world*. "HongWei worked at team X from 2023 to 2025."
    * **system time** -- ``recorded_at`` / ``invalidated_at``: when Shovel
      learned and un-learned it.

    Keeping both is what lets the agent answer "why did we previously believe
    X?" after X has been replaced. Supersession therefore NEVER deletes a row;
    it sets ``status='superseded'`` and points ``superseded_by`` at the winner.
    """

    valid_from: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    valid_until: Mapped[int | None] = mapped_column(sa.Integer)      # NULL = open-ended
    recorded_at: Mapped[int] = mapped_column(sa.Integer, default=now_ts, nullable=False)
    invalidated_at: Mapped[int | None] = mapped_column(sa.Integer)
    superseded_by: Mapped[str | None] = mapped_column(sa.Text)
    supersede_reason: Mapped[str | None] = mapped_column(sa.Text)


class SalienceMixin:
    """Signals that decide whether a memory is worth recalling at all.

    ``importance`` is assigned at write time; ``access_count`` / ``last_accessed_at``
    accumulate at read time. Together they implement retrieval-based decay:
    frequently used memories stay salient, one-off trivia fades without ever
    being deleted.
    """

    importance: Mapped[float] = mapped_column(sa.Float, default=0.5, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.7, nullable=False)
    access_count: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_accessed_at: Mapped[int | None] = mapped_column(sa.Integer)
    is_pinned: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
