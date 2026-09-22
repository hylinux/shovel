from __future__ import annotations

import json
import time
import uuid
from typing import Any

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


