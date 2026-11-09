"""Atomic settings and deterministic, revisioned Profile Memory projection."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import warnings
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.base import now_ts
from shovel.db.model.memory import MemoryBelief
from shovel.db.model.user_profile import UserProfile
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.enums import MemoryPredicate, MemorySource, MemoryValidityState, VectorState
from shovel.memory.domain import PROFILE_FIELDS, PROFILE_TOPICS, MemoryCandidate, MemoryScope, MemoryType
from shovel.memory.sqlite_repository import queue_index_intent
from shovel.runtime.errors import NotFound, RuntimeFailure

MAX_AVATAR_BYTES = 2 * 1024 * 1024


class UserProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision: int = Field(ge=0)
    nickname: str = Field(max_length=80)
    gender: str = Field(max_length=80)
    occupation: str = Field(max_length=200)
    bio: str = Field(max_length=1000)
    avatar_data: str | None = Field(default=None, max_length=2_800_000)


def decode_avatar(value: str) -> bytes:
    prefix, separator, encoded = value.partition(",")
    if not separator or prefix not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/webp;base64",
    }:
        raise RuntimeFailure("Choose a PNG, JPEG or WebP avatar.", status=415)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeFailure("Invalid avatar image.", status=422) from exc
    if len(data) > MAX_AVATAR_BYTES:
        raise RuntimeFailure("Avatar must be at most 2 MB.", status=413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise RuntimeFailure("Choose a PNG, JPEG or WebP avatar.", status=415)
                if image.width * image.height > 4_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise RuntimeFailure("Avatar must be a static image of at most 4 megapixels.", status=422)
                image.load()
                cleaned = ImageOps.exif_transpose(image).convert("RGBA")
                cleaned.thumbnail((256, 256))
                cleaned.info.clear()
                output = io.BytesIO()
                cleaned.save(output, format="PNG")
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise RuntimeFailure("Invalid avatar image.", status=422) from exc


class UserProfileService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], owner: MemoryScope) -> None:
        self.sessions, self.owner = sessions, owner

    def view(self, row: UserProfile | None) -> dict[str, Any]:
        return {
            "revision": row.revision if row else 0,
            "nickname": row.nickname if row else "",
            "gender": row.gender if row else "",
            "occupation": row.occupation if row else "",
            "bio": row.bio if row else "",
            "avatar_url": f"/api/v1/user/profile/avatar/{row.avatar_hash}" if row and row.avatar_hash else "",
            "updated_at": row.updated_at if row else None,
        }

    async def read(self) -> dict[str, Any]:
        async with self.sessions() as session:
            return self.view(await session.get(UserProfile, (self.owner.workspace_id, self.owner.user_id)))

    async def avatar(self, digest: str) -> bytes:
        async with self.sessions() as session:
            row = await session.get(UserProfile, (self.owner.workspace_id, self.owner.user_id))
            if row is None or row.avatar_hash != digest or row.avatar is None:
                raise NotFound("Avatar", digest)
            return row.avatar

    async def save(self, body: UserProfileInput) -> dict[str, Any]:
        avatar = (
            await asyncio.to_thread(decode_avatar, body.avatar_data) if body.avatar_data is not None else None
        )
        async with sqlite_write(self.sessions) as session:
            row = await session.get(UserProfile, (self.owner.workspace_id, self.owner.user_id))
            if body.revision != (row.revision if row else 0):
                raise RuntimeFailure("Profile changed. Reload saved profile before saving again.", status=409)
            if row is None:
                row = UserProfile(
                    workspace_id=self.owner.workspace_id, user_id=self.owner.user_id, revision=0
                )
                session.add(row)
            old = self.view(row)
            for field in ("nickname", "gender", "occupation", "bio"):
                setattr(row, field, getattr(body, field))
            if "avatar_data" in body.model_fields_set:
                row.avatar = avatar
                row.avatar_hash = hashlib.sha256(avatar).hexdigest() if avatar else ""
            if row.revision == 0 or any(self.view(row)[key] != old[key] for key in PROFILE_FIELDS.values()):
                row.revision += 1
                row.updated_at = now_ts()
            await session.flush()
            await self._sync(session, row)
            await session.commit()
            return self.view(row)

    async def synchronize(self) -> None:
        async with sqlite_write(self.sessions) as session:
            row = await session.get(UserProfile, (self.owner.workspace_id, self.owner.user_id))
            if row is not None:
                await self._sync(session, row)
            await session.commit()

    async def _sync(self, session: AsyncSession, profile: UserProfile) -> None:
        values = self.view(profile)
        rows = list(
            await session.scalars(
                select(MemoryBelief).where(
                    MemoryBelief.workspace_id == self.owner.workspace_id,
                    MemoryBelief.user_id == self.owner.user_id,
                    MemoryBelief.subject == "user",
                    MemoryBelief.predicate.in_([MemoryPredicate(p) for p in PROFILE_FIELDS]),
                )
            )
        )
        for predicate, field in PROFILE_FIELDS.items():
            identifier = (
                "profile_"
                + hashlib.sha256(
                    f"{self.owner.workspace_id}|{self.owner.user_id}|{field}".encode()
                ).hexdigest()
            )
            current = next((row for row in rows if row.id == identifier), None)
            value = str(values[field])
            candidate = MemoryCandidate(
                text=f"{PROFILE_TOPICS[predicate]}: {value}",
                predicate=predicate,
                object=value,
                topic=PROFILE_TOPICS[predicate],
            )
            for previous in rows:
                if (
                    previous.id != identifier
                    and previous.predicate == predicate
                    and previous.validity_state in (MemoryValidityState.ACTIVE, MemoryValidityState.CANDIDATE)
                ):
                    previous.validity_state = MemoryValidityState.SUPERSEDED
                    previous.invalidated_at = now_ts()
                    previous.supersede_reason = "User-maintained Profile settings"
                    previous.superseded_by = identifier if value else None
                    queue_index_intent(session, previous, MemoryType.SEMANTIC, "delete")
            await session.flush()
            if current is None and not value:
                continue
            if current is not None:
                target_state = MemoryValidityState.ACTIVE if value else MemoryValidityState.DELETED
                if current.object_ == value and current.validity_state == target_state:
                    continue
                queue_index_intent(session, current, MemoryType.SEMANTIC, "delete")
                current.rev = max(current.rev + 1, profile.revision)
            else:
                current = MemoryBelief(
                    id=identifier,
                    workspace_id=self.owner.workspace_id,
                    user_id=self.owner.user_id,
                    scope_kind="user",
                    agent_id="",
                    session_id="",
                    rev=profile.revision,
                    subject="user",
                    predicate=MemoryPredicate(predicate),
                )
                session.add(current)
            current.object_ = value
            current.object_domain = candidate.topic
            current.note = candidate.text
            current.fingerprint = candidate.fingerprint
            current.source = MemorySource.STATED
            current.confidence = current.importance = 1.0
            current.validity_state = MemoryValidityState.ACTIVE if value else MemoryValidityState.DELETED
            current.invalidated_at = None if value else now_ts()
            current.valid_until = None
            current.superseded_by = None
            current.vector_state = VectorState.PENDING
            current.model_version = None
            current.provenance = [
                {
                    "source_kind": "user_settings",
                    "field": field,
                    "profile_revision": profile.revision,
                    "asserted_at": profile.updated_at,
                }
            ]
            await session.flush()
            if value:
                queue_index_intent(session, current, MemoryType.SEMANTIC, "upsert")
