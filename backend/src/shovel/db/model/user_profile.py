"""User-maintained profile; avatar bytes never enter Memory or model requests."""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, now_ts


class UserProfile(Base):
    __tablename__ = "user_profile"

    workspace_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    nickname: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    gender: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    occupation: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    bio: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    avatar: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    avatar_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    updated_at: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=now_ts)
