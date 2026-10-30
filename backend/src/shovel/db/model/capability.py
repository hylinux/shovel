"""Capability identity and immutable published content, without execution state."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import DDL, event
from sqlalchemy.orm import Mapped, mapped_column

from shovel.db.base import Base, JSONDict, new_id, now_ts

_AVAILABILITY = "availability IN ('active', 'deprecated', 'disabled', 'archived')"


class CapabilityDefinition(Base):
    __tablename__ = "capability_definition"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("cap"))
    workspace_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    availability: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")
    created_at: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=now_ts)

    __table_args__ = (
        sa.CheckConstraint("kind IN ('function', 'skill', 'workflow', 'agent')", name="capability_kind"),
        sa.CheckConstraint(_AVAILABILITY, name="definition_availability"),
        sa.CheckConstraint("length(workspace_id) > 0 AND length(user_id) > 0", name="capability_owner"),
        sa.Index("idx_capability_owner", "workspace_id", "user_id", "kind", "name"),
    )


class CapabilityVersion(Base):
    __tablename__ = "capability_version"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True, default=lambda: new_id("capv"))
    capability_id: Mapped[str] = mapped_column(
        sa.ForeignKey("capability_definition.id", ondelete="RESTRICT"),
        nullable=False,
    )
    number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSONDict, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=now_ts)
    published_at: Mapped[int | None] = mapped_column(sa.Integer)
    published_by: Mapped[str | None] = mapped_column(sa.Text)
    based_on_version_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("capability_version.id", ondelete="RESTRICT"),
    )

    __table_args__ = (
        sa.UniqueConstraint("capability_id", "number", name="uq_capability_version_number"),
        sa.CheckConstraint("number > 0 AND revision > 0", name="capability_version_positive"),
        sa.CheckConstraint(
            "(published_at IS NULL AND content_hash IS NULL AND published_by IS NULL) OR "
            "(published_at IS NOT NULL AND content_hash IS NOT NULL AND length(content_hash) = 64 "
            "AND published_by IS NOT NULL)",
            name="capability_publication",
        ),
    )


class CapabilityVersionAvailability(Base):
    __tablename__ = "capability_version_availability"

    version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("capability_version.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    availability: Mapped[str] = mapped_column(sa.Text, nullable=False, default="active")

    __table_args__ = (sa.CheckConstraint(_AVAILABILITY, name="version_availability"),)


class CapabilityDependency(Base):
    __tablename__ = "capability_dependency"

    owner_version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("capability_version.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    target_version_id: Mapped[str] = mapped_column(
        sa.ForeignKey("capability_version.id", ondelete="RESTRICT"),
        primary_key=True,
    )

    __table_args__ = (sa.Index("idx_capability_dependency_target", "target_version_id"),)


CAPABILITY_DDL = {
    CapabilityDefinition.__table__: (
        """CREATE TRIGGER IF NOT EXISTS capability_identity_replace
        BEFORE INSERT ON capability_definition
        WHEN EXISTS (SELECT 1 FROM capability_definition WHERE id = NEW.id)
        BEGIN SELECT RAISE(ABORT, 'Capability identity cannot be replaced'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_identity_immutable
        BEFORE UPDATE OF id, workspace_id, user_id, kind ON capability_definition
        WHEN NEW.id IS NOT OLD.id OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.user_id IS NOT OLD.user_id OR NEW.kind IS NOT OLD.kind
        BEGIN SELECT RAISE(ABORT, 'Capability identity is immutable'); END""",
    ),
    CapabilityVersion.__table__: (
        """CREATE TRIGGER IF NOT EXISTS capability_published_replace
        BEFORE INSERT ON capability_version
        WHEN EXISTS (SELECT 1 FROM capability_version WHERE published_at IS NOT NULL
                     AND (id = NEW.id OR (capability_id = NEW.capability_id AND number = NEW.number)))
        BEGIN SELECT RAISE(ABORT, 'Published capability version is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_published_update
        BEFORE UPDATE ON capability_version WHEN OLD.published_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'Published capability version is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_published_delete
        BEFORE DELETE ON capability_version WHEN OLD.published_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'Published capability version is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_version_identity
        BEFORE UPDATE OF id, capability_id, number ON capability_version
        WHEN NEW.id IS NOT OLD.id OR NEW.capability_id IS NOT OLD.capability_id
          OR NEW.number IS NOT OLD.number
        BEGIN SELECT RAISE(ABORT, 'Capability version identity is immutable'); END""",
    ),
    CapabilityVersionAvailability.__table__: (
        """CREATE TRIGGER IF NOT EXISTS capability_availability_identity
        BEFORE UPDATE OF version_id ON capability_version_availability
        WHEN NEW.version_id IS NOT OLD.version_id
        BEGIN SELECT RAISE(ABORT, 'Capability availability identity is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_availability_delete
        BEFORE DELETE ON capability_version_availability
        WHEN EXISTS (SELECT 1 FROM capability_version WHERE id = OLD.version_id AND published_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Published capability availability must be retained'); END""",
    ),
    CapabilityDependency.__table__: (
        """CREATE TRIGGER IF NOT EXISTS capability_dependency_insert
        BEFORE INSERT ON capability_dependency
        WHEN EXISTS (SELECT 1 FROM capability_version
                     WHERE id = NEW.owner_version_id AND published_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Published dependency lock is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_dependency_update
        BEFORE UPDATE ON capability_dependency
        WHEN EXISTS (SELECT 1 FROM capability_version
                     WHERE id IN (OLD.owner_version_id, NEW.owner_version_id) AND published_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Published dependency lock is immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS capability_dependency_delete
        BEFORE DELETE ON capability_dependency
        WHEN EXISTS (SELECT 1 FROM capability_version
                     WHERE id = OLD.owner_version_id AND published_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'Published dependency lock is immutable'); END""",
    ),
}

for _table, _statements in CAPABILITY_DDL.items():
    for _statement in _statements:
        _ddl = DDL(_statement)  # type: ignore[no-untyped-call]
        event.listen(_table, "after_create", _ddl.execute_if(dialect="sqlite"))
