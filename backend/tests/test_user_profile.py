from __future__ import annotations

import asyncio
import base64
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_framework import Message
from PIL import Image, PngImagePlugin
from sqlalchemy import func, select

from shovel.db.model.memory import MemoryBelief, MemoryIndexOutbox
from shovel.memory.domain import MemoryCandidate, MemoryState, MemoryType, Provenance, VectorHit
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import MessageInput
from shovel.runtime.service import Runtime
from shovel.runtime.user_profile import UserProfileService
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_native_memory import vectors


def profile(**changes: Any) -> dict[str, Any]:
    return {
        "revision": 0,
        "nickname": "River",
        "gender": "not specified",
        "occupation": "Engineer",
        "bio": "I enjoy reading.",
        **changes,
    }


def avatar() -> str:
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "must be removed")
    exif = Image.Exif()
    exif[270] = "private EXIF metadata"
    Image.new("RGB", (512, 512), "blue").save(output, format="PNG", pnginfo=metadata, exif=exif)
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


async def test_profile_atomic_persistence_avatar_and_idempotent_startup(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    assert (await api.get("/api/v1/user/profile")).json()["revision"] == 0
    response = await api.put("/api/v1/user/profile", json=profile(avatar_data=avatar()))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["revision"] == 1
    image = await api.get(saved["avatar_url"])
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert b"must be removed" not in image.content
    assert b"private EXIF" not in image.content
    with Image.open(io.BytesIO(image.content)) as decoded:
        assert decoded.size == (256, 256)
    owner = runtime.memory.owner
    for _ in range(2):
        await runtime.user_profile.synchronize()
        await runtime.chat.restart_agent()
        await runtime.chat.agent.load_instructions()
    repeat = await api.put("/api/v1/user/profile", json=profile(revision=1))
    assert repeat.json() == saved
    assert await UserProfileService(runtime.sessions, owner).read() == saved
    async with runtime.sessions() as session:
        rows = list(await session.scalars(select(MemoryBelief)))
        assert len(rows) == 5
        assert {row.rev for row in rows} == {1}
        assert all(row.scope_kind == "user" for row in rows)
        assert all(row.provenance[0]["source_kind"] == "user_settings" for row in rows)
        assert await session.scalar(select(func.count()).select_from(MemoryIndexOutbox)) == 5


async def test_profile_changes_clear_old_facts_and_reject_stale_vectors(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    first = (await api.put("/api/v1/user/profile", json=profile(avatar_data=avatar()))).json()
    old = await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
    name = next(row for row in old if row.predicate == "has_name")
    updated = (
        await api.put(
            "/api/v1/user/profile",
            json=profile(revision=1, nickname="Lake", gender="", occupation="", bio="", avatar_data=None),
        )
    ).json()
    assert updated["revision"] == 2 and not updated["avatar_url"]
    assert (await api.get(first["avatar_url"])).status_code == 404
    current = await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
    assert [(row.id, row.object, row.revision) for row in current] == [(name.id, "Lake", 2)]

    class StaleIndex:
        model_version = "old-model"

        async def search(self, *args: Any) -> list[VectorHit]:
            return [VectorHit(row.id, MemoryType.SEMANTIC, row.revision, 1.0) for row in old]

    runtime.memory.application.index = StaleIndex()
    found = await runtime.memory.application.retrieve("What is my occupation?")
    assert not any(item["object"] in {"River", "Engineer", "I enjoy reading."} for item in found)
    async with runtime.sessions() as session:
        intents = list(await session.scalars(select(MemoryIndexOutbox)))
        assert sum(row.action == "delete" for row in intents) == 5
        assert any(
            row.memory_id == name.id and row.revision == 2 and row.action == "upsert" for row in intents
        )


async def test_profile_overrides_chat_conflicts_and_clearing_does_not_resurrect_them(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    candidate = MemoryCandidate("My name is Old.", predicate="has_name", object="Old", topic="user.name")
    old = (await repo.consolidate(scope, [candidate], Provenance("manual")))[0]
    saved = (await api.put("/api/v1/user/profile", json=profile())).json()
    assert (await repo.get(scope, old.id)).state == MemoryState.SUPERSEDED
    assert await repo.consolidate(scope, [candidate], Provenance("user_message")) == []
    with pytest.raises(RuntimeFailure, match="General settings"):
        await repo.consolidate(scope, [candidate], Provenance("manual"))
    managed = next(row for row in await repo.profile(scope, limit=10) if row.predicate == "has_name")
    with pytest.raises(RuntimeFailure, match="General settings"):
        await repo.transition(scope, managed.id, MemoryState.DELETED)
    with pytest.raises(RuntimeFailure, match="General settings"):
        await repo.consolidate(
            managed.scope,
            [MemoryCandidate("User prefers tea.", predicate="prefers", object="tea", topic="drink")],
            Provenance("manual"),
            replace_id=managed.id,
        )
    answer = await runtime.chat.agent.profile_response(
        "session",
        MessageInput(content="What is my nickname?"),
        [Message("user", "My name is Old.")],
        None,
    )
    assert answer is not None and "River" in answer.content and "Old" not in answer.content
    await api.put("/api/v1/user/profile", json=profile(revision=saved["revision"], nickname=""))
    assert await repo.consolidate(scope, [candidate], Provenance("user_message")) == []
    assert not any(row.predicate == "has_name" for row in await repo.profile(scope, limit=10))
    await runtime.user_profile.synchronize()
    assert not any(row.predicate == "has_name" for row in await repo.profile(scope, limit=10))
    other = await repo.consolidate(replace(scope, user_id="another-user"), [candidate], Provenance("manual"))
    assert len(other) == 1
    assert (await UserProfileService(runtime.sessions, replace(scope, user_id="another-user")).read())[
        "revision"
    ] == 0
    assert (
        await UserProfileService(runtime.sessions, replace(scope, workspace_id="another-workspace")).read()
    )["revision"] == 0


async def test_profile_revision_conflicts_and_sync_failure_roll_back(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = await asyncio.gather(
        api.put("/api/v1/user/profile", json=profile()),
        api.put("/api/v1/user/profile", json=profile(nickname="Other")),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    saved = (await api.get("/api/v1/user/profile")).json()
    original = runtime.user_profile._sync

    async def fail(*args: Any) -> None:
        await original(*args)
        raise RuntimeFailure("Simulated transaction failure")

    monkeypatch.setattr(runtime.user_profile, "_sync", fail)
    failed = await api.put("/api/v1/user/profile", json=profile(revision=1, nickname="Must roll back"))
    assert failed.status_code == 400
    assert (await api.get("/api/v1/user/profile")).json() == saved
    rows = await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
    assert next(row.object for row in rows if row.predicate == "has_name") == saved["nickname"]


@pytest.mark.parametrize(
    "data,status",
    [
        ("data:image/svg+xml;base64,PHN2Zy8+", 415),
        ("data:image/png;base64,bm90LWFuLWltYWdl", 422),
        ("data:image/png;base64,!!!", 422),
        ("", 415),
        ("data:image/png;base64," + base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode(), 413),
    ],
)
async def test_profile_rejects_invalid_avatar_without_saving(
    api: httpx.AsyncClient, data: str, status: int
) -> None:
    response = await api.put("/api/v1/user/profile", json=profile(avatar_data=data))
    assert response.status_code == status
    assert (await api.get("/api/v1/user/profile")).json()["revision"] == 0


async def test_current_profile_reaches_model_without_avatar_or_automatic_memory(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    captured: list[dict[str, Any]] = []

    def model(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "Hello, River!"}}],
            },
        )

    await api.put("/api/v1/user/profile", json=profile(avatar_data=avatar()))
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    for nickname in ("River", "Lake"):
        if nickname == "Lake":
            await api.put("/api/v1/user/profile", json=profile(revision=1, nickname=nickname))
        response = await api.post(
            f"/api/v1/conversations/{identifier}/messages",
            json={
                "content": "Hello",
                "memory_enabled": False,
            },
        )
        assert response.status_code == 200, response.text
        entries = [
            item["content"]
            for item in captured[-1]["messages"]
            if "user_profile" in item["content"] and item["role"] == "user"
        ]
        assert len(entries) == 1
        data = json.loads(entries[0].split("\n", 1)[1])["user_profile"]
        assert data["nickname"] == nickname
        assert data["occupation"] == "Engineer"
        assert "avatar" not in entries[0] and "base64" not in entries[0]
        assert "user_profile.nickname" in captured[-1]["messages"][0]["content"]


async def test_profile_vector_projection_flushes_updates_and_clears(
    runtime: Runtime, api: httpx.AsyncClient, tmp_path: Path
) -> None:
    index = vectors(runtime, tmp_path)
    await api.put("/api/v1/user/profile", json=profile())
    assert await runtime.memory.maintain()
    initial = await index.search("River", runtime.memory.owner, 10)
    assert len(initial) == 4
    await api.put(
        "/api/v1/user/profile",
        json=profile(revision=1, nickname="Lake", gender="", occupation="", bio=""),
    )
    assert await runtime.memory.maintain()
    hits = await index.search("Lake", runtime.memory.owner, 10)
    assert len(hits) == 1
    current = await runtime.memory.repository.get(runtime.memory.owner, hits[0].id)
    assert current.object == "Lake" and hits[0].revision == current.revision == 2
    assert await runtime.memory.repository.pending(runtime.memory.owner, force=True) == []


@pytest.mark.parametrize("animated", [False, True])
async def test_profile_rejects_excessive_pixels_and_animation(api: httpx.AsyncClient, animated: bool) -> None:
    output = io.BytesIO()
    if animated:
        Image.new("RGB", (4, 4), "red").save(
            output, format="PNG", save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")]
        )
    else:
        Image.new("RGB", (2001, 2000)).save(output, format="PNG")
    data = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
    response = await api.put("/api/v1/user/profile", json=profile(avatar_data=data))
    assert response.status_code == 422
    assert (await api.get("/api/v1/user/profile")).json()["revision"] == 0
