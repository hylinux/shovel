from __future__ import annotations

import json
import time
import uuid

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



def chunk_vector_id(doc_uri: str, content_hash: str, chunker_version: str,
                    granularity: str, index: int) -> str:
    """根据Chunk的内容生成唯一的ID

    根据该规则生成的向量id, 无论是在SQLite里, 还是在Zvec里，他们的ID都是相同的。

    一个key 两个存储。

                     chunk_vector_id(...)
                         │
          ┌──────────────┴──────────────┐
          ↓                             ↓
   SQLite: chunk.id              Zvec: Doc.id
   ├─ text（权威全文）            ├─ dense 向量
   ├─ chunk_index                 ├─ 过滤用的标量字段
   └─ parent_chunk_id             └─ FTS 文本

   这两个 id 是同一个值。 由此带来三件事：

    | 操作	| 怎么做 |
    | --- | --- |
    | join |	Zvec 返回一批 id → 直接 WHERE chunk.id IN (...) 取全文 |
    | 对账	| 两边各拉一份 id 集合，做差集就知道谁多了谁少了 |
    | 同删	| 一个 id 列表，SQLite 删一次、Zvec 删一次 |

    如果两边 id 不同，你就需要第三张映射表 chunk_id ↔ vector_id。而那张表会立刻成为新的故障点：它自己可能
    不一致、可能落后、可能在崩溃时半写。
    这正是"SQLite 是真相之源，Zvec 可丢弃重建"能成立的技术前提——删掉 Zvec 重建时，id 能被重新算出来，
    不需要从映射表里恢复。
    """
    key = f"{doc_uri}|{content_hash}|{chunker_version}|{granularity}|{index}"
    return str(uuid.uuid5(SHOVEL_NAMESPACE, key))


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
    """A JSON value stored in a TEXT column.

    Subclasses override :meth:`empty_value` so a NULL column round-trips to
    ``[]`` or ``{}`` rather than ``None`` -- callers then never need a None
    check.

    The empty value comes from a METHOD, never a class attribute. A bare
    ``empty = []`` would be a single list shared by every column of that type in
    the process: the first caller to ``append`` to a decoded-empty value would
    mutate the class attribute itself, and every later read would start from the
    polluted list. A method constructs a new object on every call, so the whole
    class of bug is unreachable rather than merely avoided.
    """

    impl = sa.Text
    cache_ok = True

    def empty_value(self) -> Any:
        """The value a NULL or empty column decodes to.

        Override in subclasses. Returning a literal here is safe precisely
        because this is a method: each call builds a fresh object.
        """
        return None

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            value = self.empty_value()
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        # Handles every subclass, so none of them override this -- the
        # duplication that used to live here only existed to dodge the shared
        # mutable default.
        if not value:
            return self.empty_value()
        return json.loads(value)

class JSONList(JSONEncoded):
    """JSON array column; a NULL column decodes to a fresh ``[]``."""

    cache_ok = True

    def empty_value(self) -> list[Any]:
        return []


class JSONDict(JSONEncoded):
    """JSON object column; a NULL column decodes to a fresh ``{}``."""

    cache_ok = True

    def empty_value(self) -> dict[str, Any]:
        return {}


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
