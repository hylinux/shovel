from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from shovel.db.base import now_ts
from shovel.db.model.config import ScanProfile
from shovel.db.model.content import Chunk, Document, ProfileDocument
from shovel.db.model.execution import Job
from shovel.db.model.retrieval import DeletionOutbox
from shovel.domain.enums import (
    DeletionScope,
    DocumentLifecycleState,
    DocumentPipeState,
    JobState,
    ScanMode,
    VectorState,
)
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import ProfileInput
from shovel.services.resource_service import DEFAULT_PROFILE_NAME

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


def profile_view(profile: ScanProfile) -> dict[str, Any]:
    return {
        name: getattr(profile, name)
        for name in (
            "id",
            "name",
            "resource_id",
            "scan_mode",
            "is_enabled",
            "include",
            "exclude",
            "max_file_bytes",
            "time_window_days",
            "pipeline",
        )
    }


async def change_profile(
    runtime: Runtime,
    resource_id: str,
    name: str,
    payload: dict[str, Any] | None,
    *,
    expected_id: str | None = None,
) -> dict[str, Any]:
    await runtime.resources.get(resource_id)
    if not name or len(name) > 200 or "/" in name or "\\" in name:
        raise RuntimeFailure("Profile 名称必须为 1-200 个字符,且不能包含路径分隔符。")
    async with runtime.sessions() as session:
        active = await session.scalar(
            select(Job.id)
            .where(
                Job.resource_id == resource_id,
                Job.state.in_((JobState.QUEUED, JobState.RUNNING)),
            )
            .limit(1)
        )
        if active:
            raise RuntimeFailure("资源还有排队或运行中的扫描,请等待或取消后再修改 Profile。", status=409)
        statement = select(ScanProfile).where(ScanProfile.resource_id == resource_id)
        statement = (
            statement.where(ScanProfile.name == name)
            if payload is not None
            else statement.where(ScanProfile.id == expected_id)
        )
        profile = await session.scalar(statement)
        if payload is not None:
            request = ProfileInput.model_validate(payload)
            connector = await runtime.resources.build_connector(resource_id)
            if not connector.spec.supports_mode(ScanMode(request.scan_mode)):
                raise RuntimeFailure("连接器不支持此 Profile 的扫描模式。")
            if profile is None:
                profile = ScanProfile(resource_id=resource_id, name=name)
                session.add(profile)
            for key, value in request.model_dump(exclude_none=True).items():
                setattr(profile, key, ScanMode(value) if key == "scan_mode" else value)
            profile.time_window_days = request.time_window_days
            await session.commit()
            return profile_view(profile)
        if name == DEFAULT_PROFILE_NAME:
            raise RuntimeFailure("不能删除默认 Profile;可停用或修改它。", status=409)
        if profile is None:
            return {"name": name, "profile_id": expected_id, "deleted": True}
        candidates = select(ProfileDocument.doc_uri).where(ProfileDocument.profile_id == profile.id)
        uris = list(await session.scalars(candidates))
        await session.execute(delete(ProfileDocument).where(ProfileDocument.profile_id == profile.id))
        await session.delete(profile)
        await session.flush()
        orphaned = list(
            await session.scalars(
                select(Document.uri).where(
                    Document.resource_id == resource_id,
                    Document.uri.in_(uris),
                    ~select(ProfileDocument.doc_uri)
                    .where(
                        ProfileDocument.resource_id == Document.resource_id,
                        ProfileDocument.doc_uri == Document.uri,
                    )
                    .exists(),
                )
            )
        )
        if orphaned:
            await session.execute(
                update(Document)
                .where(
                    Document.resource_id == resource_id,
                    Document.uri.in_(orphaned),
                )
                .values(
                    lifecycle_state=DocumentLifecycleState.TOMBSTONED,
                    tombstoned_at=now_ts(),
                    pipe_state=DocumentPipeState.PENDING,
                    skip_reason="所属扫描 Profile 已删除。",
                )
            )
            await session.execute(
                update(Chunk)
                .where(
                    Chunk.resource_id == resource_id,
                    Chunk.doc_uri.in_(orphaned),
                )
                .values(vector_state=VectorState.PENDING)
            )
            session.add_all(
                [
                    DeletionOutbox(
                        scope=DeletionScope.DOC,
                        resource_id=resource_id,
                        doc_uri=uri,
                    )
                    for uri in orphaned
                ]
            )
        await session.commit()
        return {"name": name, "profile_id": expected_id, "deleted": True, "tombstoned": len(orphaned)}
