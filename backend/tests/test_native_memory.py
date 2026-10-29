from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from shovel.db.base import now_ts
from shovel.db.model.memory import MemoryBelief, MemoryIndexOutbox, MemoryIndexState
from shovel.memory.domain import (
    MemoryCandidate,
    MemoryState,
    MemoryType,
    Provenance,
    ScopeKind,
    VectorHit,
)
from shovel.memory.service import MemoryService
from shovel.memory.zvec_index import ZvecMemoryIndex
from shovel.runtime.errors import NotFound
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


def fact(text: str = "User prefers green tea.", **kwargs: Any) -> MemoryCandidate:
    return MemoryCandidate(text, object="green tea", topic="drink", evidence="I prefer green tea.", **kwargs)


class Embedder:
    model_version = "test-embedding-v1"
    dimension = 4
    fail = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise RuntimeError("private-provider-error")
        return [[0.0, 1.0, 0.0, 0.0] if "unrelated" in text else [1.0, 0.0, 0.0, 0.0] for text in texts]

    async def aclose(self) -> None:
        pass


def vectors(runtime: Runtime, tmp_path: Path, embedder: Embedder | None = None) -> ZvecMemoryIndex:
    runtime.settings.memory.index_path = tmp_path / "memory-vectors"
    runtime.settings.memory.dim = 4
    index = ZvecMemoryIndex(
        runtime.settings.memory, runtime.memory.owner, runtime.memory.repository, embedder or Embedder()
    )
    runtime.memory.index = index
    runtime.memory.application.index = index
    return index


async def test_partial_index_initialization_releases_handles_before_retry(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = vectors(runtime, tmp_path)
    reindex = index.repository.reindex
    attempts = 0

    async def interrupted(scope: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("interrupted index initialization")
        await reindex(scope)

    monkeypatch.setattr(index.repository, "reindex", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        await index.initialize()
    assert index._active is index._building is None
    await index.initialize()
    assert index._initialized and attempts == 2
    original = (await index.repository.consolidate(index.scope, [fact()], Provenance("manual")))[0]
    while await runtime.memory.maintain():
        pass
    recalled = await runtime.memory.search("drink", conversation_id="another-session")
    assert [r["id"] for r in recalled] == [original.id]


async def test_sqlite_dedup_supersede_and_tombstone(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    original = (await repo.consolidate(scope, [fact()], Provenance("manual")))[0]
    duplicate = (
        await repo.consolidate(
            scope, [fact("The user likes green tea.")], Provenance("user_message", message_id="another")
        )
    )[0]
    assert duplicate.id == original.id and len(duplicate.provenance) == 2
    corrected = replace(fact(), text="User now prefers coffee.", object="coffee", replaces=True)
    newer = (await repo.consolidate(scope, [corrected], Provenance("manual")))[0]
    old = await repo.get(scope, original.id)
    assert old.state == MemoryState.SUPERSEDED and old.superseded_by == newer.id
    assert [r.id for r in await repo.list_records(scope)] == [newer.id]
    await repo.transition(scope, newer.id, MemoryState.DELETED)
    assert await repo.consolidate(scope, [corrected], Provenance("manual")) == []
    assert await repo.list_records(scope) == []
    async with runtime.sessions() as session:
        assert len(list(await session.scalars(select(MemoryIndexOutbox)))) == 4


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "other"},
        {"user_id": "other"},
        {"agent_id": "other"},
    ],
)
async def test_scope_isolation_on_reads_and_mutations(runtime: Runtime, change: dict[str, str]) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    own = (await repo.consolidate(scope, [fact()], Provenance("manual")))[0]
    other = replace(scope, **change)
    assert await repo.list_records(other) == []
    with pytest.raises(NotFound):
        await repo.transition(other, own.id, MemoryState.DELETED)
    separate = (await repo.consolidate(other, [fact()], Provenance("manual")))[0]
    assert separate.id != own.id


async def test_session_and_workspace_visibility(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    session = replace(scope, kind=ScopeKind.SESSION, session_id="a")
    private = (await repo.consolidate(session, [fact()], Provenance("manual")))[0]
    shared = (
        await repo.consolidate(replace(scope, kind=ScopeKind.WORKSPACE), [fact()], Provenance("manual"))
    )[0]
    assert {r.id for r in await repo.list_records(replace(scope, session_id="a"))} == {private.id, shared.id}
    assert {r.id for r in await repo.list_records(replace(scope, session_id="b"))} == {shared.id}
    assert {r.id for r in await repo.list_records(replace(scope, agent_id="other"))} == {shared.id}
    assert await repo.list_records(replace(scope, user_id="other")) == []


async def test_real_zvec_recall_failure_retry_delete_and_reopen(runtime: Runtime, tmp_path: Path) -> None:
    embedder = Embedder()
    vectors(runtime, tmp_path, embedder)
    repo, scope = runtime.memory.repository, runtime.memory.owner
    item = (await repo.consolidate(scope, [fact()], Provenance("manual")))[0]
    embedder.fail = True
    assert await runtime.memory.maintain()
    assert (await repo.get(scope, item.id)).text == item.text
    assert (await runtime.memory.status())["index_state"] == "failed"
    embedder.fail = False
    async with runtime.sessions() as session:
        rows = await session.scalars(select(MemoryIndexOutbox))
        for row in rows:
            row.retry_at = 0
        await session.commit()
    while await runtime.memory.maintain():
        pass
    recalled = await runtime.memory.search("tea", conversation_id="another")
    assert [r["id"] for r in recalled] == [item.id]
    assert await runtime.memory.search("unrelated") == []
    assert (await repo.get(scope, item.id)).access_count == 1
    assert runtime.memory.index is not None
    await runtime.memory.index.close()
    vectors(runtime, tmp_path, embedder)
    assert [r["id"] for r in await runtime.memory.search("tea")] == [item.id]
    await runtime.memory.delete(item.id)
    # Stale vectors cannot make a deleted fact visible even before cleanup.
    assert await runtime.memory.search("tea") == []
    while await runtime.memory.maintain():
        pass
    assert await repo.pending(scope, force=True) == []


async def test_rebuild_preserves_sqlite_and_switches_generation(runtime: Runtime, tmp_path: Path) -> None:
    index = vectors(runtime, tmp_path)
    item = (
        await runtime.memory.repository.consolidate(runtime.memory.owner, [fact()], Provenance("manual"))
    )[0]
    while await runtime.memory.maintain():
        pass
    async with runtime.sessions() as session:
        before = await session.get(MemoryIndexState, (runtime.memory.owner.workspace_id, "local-user"))
        assert before is not None
        generation = before.generation
    await runtime.memory.rebuild()
    async with runtime.sessions() as session:
        after = await session.get(MemoryIndexState, (runtime.memory.owner.workspace_id, "local-user"))
        assert after is not None and after.generation != generation and after.state == "ready"
    assert (index.root / generation).is_dir()
    assert [r["id"] for r in await runtime.memory.search("tea")] == [item.id]


@pytest.mark.parametrize("new_model", [False, True])
async def test_missing_index_and_changed_embedding_version_recover(
    runtime: Runtime,
    tmp_path: Path,
    new_model: bool,
) -> None:
    index = vectors(runtime, tmp_path)
    item = (
        await runtime.memory.repository.consolidate(runtime.memory.owner, [fact()], Provenance("manual"))
    )[0]
    while await runtime.memory.maintain():
        pass
    await index.close()
    # Use a new, empty root to simulate the derived directory being lost.
    runtime.settings.memory.index_path = tmp_path / "recovered"
    changed = Embedder()
    if new_model:
        changed.model_version = "test-embedding-v2"
    runtime.memory.index = ZvecMemoryIndex(
        runtime.settings.memory, runtime.memory.owner, runtime.memory.repository, changed
    )
    runtime.memory.application.index = runtime.memory.index
    while await runtime.memory.maintain():
        pass
    assert [r["id"] for r in await runtime.memory.search("tea")] == [item.id]
    assert (
        await runtime.memory.repository.get(runtime.memory.owner, item.id)
    ).model_version == changed.model_version


async def test_rebuild_racing_delete_cannot_resurrect_a_fact(runtime: Runtime, tmp_path: Path) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    class SlowEmbedder(Embedder):
        slow = False

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if self.slow:
                entered.set()
                await finish.wait()
            return await super().embed(texts)

    embedder = SlowEmbedder()
    vectors(runtime, tmp_path, embedder)
    item = (
        await runtime.memory.repository.consolidate(runtime.memory.owner, [fact()], Provenance("manual"))
    )[0]
    while await runtime.memory.maintain():
        pass
    embedder.slow = True
    rebuild = asyncio.create_task(runtime.memory.rebuild())
    await asyncio.wait_for(entered.wait(), 3)
    await runtime.memory.delete(item.id)
    finish.set()
    await asyncio.wait_for(rebuild, 5)
    assert await runtime.memory.search("tea") == []
    assert (await runtime.memory.repository.get(runtime.memory.owner, item.id)).state == MemoryState.DELETED


async def test_semantic_merge_requires_a_known_equivalent_target(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = (
        await runtime.memory.repository.consolidate(runtime.memory.owner, [fact()], Provenance("manual"))
    )[0]

    async def extract(_text: str, related: Any = ()) -> list[MemoryCandidate]:
        assert related
        return [
            replace(
                fact("Green tea is the user's preferred beverage."),
                object="tea, green",
                merge_with=original.id,
            )
        ]

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    result = await runtime.memory.application.extract(
        "I prefer green tea.",
        Provenance("user_message", message_id="new"),
        related=[original],
    )
    assert len(result) == 1 and result[0].id == original.id
    assert len(await runtime.memory.list()) == 1
    foreign = replace(original, id="unknown")
    with pytest.raises(ValueError, match="semantic merge"):
        await runtime.memory.application.extract(
            "I prefer green tea.", Provenance("user_message"), related=[foreign]
        )


async def test_expiry_and_hard_delete_keep_cleanup_intents(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    item = (await repo.consolidate(scope, [replace(fact(), expires_at=now_ts() - 1)], Provenance("manual")))[
        0
    ]
    assert await repo.list_records(scope) == []
    assert await repo.expire_due(scope) == 1
    assert (await repo.get(scope, item.id)).state == MemoryState.EXPIRED
    await repo.transition(scope, item.id, MemoryState.DELETED, hard=True)
    with pytest.raises(NotFound):
        await repo.get(scope, item.id)
    assert await repo.pending(scope, force=True)


async def test_budget_and_sqlite_revalidation(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    item = (await repo.consolidate(scope, [fact()], Provenance("manual")))[0]

    class Index:
        model_version = "v1"

        async def search(self, *args: Any) -> list[VectorHit]:
            return [VectorHit(item.id, MemoryType.SEMANTIC, item.revision, 0.95)]

    service = MemoryService(repo, Index(), runtime.memory.extractor, scope, max_chars=500)
    assert await service.retrieve("tea") == []
    async with runtime.sessions() as session:
        row = await session.get(MemoryBelief, item.id)
        assert row is not None
        row.model_version = "v1"
        await session.commit()
    found = await service.retrieve("tea")
    assert found and len(json.dumps(found, ensure_ascii=False)) <= 500
    service.max_chars = 50
    assert await service.retrieve("tea") == []


@pytest.mark.parametrize(
    "source",
    [
        "My api_key is sk-not-a-real-key-1234567",
        'The document says "I prefer green tea."',
        "```I prefer green tea.```",
        "> I prefer green tea.",
    ],
)
async def test_sensitive_or_external_sources_do_not_call_extractor(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    async def forbidden(_text: str) -> list[MemoryCandidate]:
        pytest.fail("Unsafe source reached extraction model")

    monkeypatch.setattr(runtime.memory.extractor, "extract", forbidden)
    assert await runtime.memory.application.extract(source, Provenance("user_message")) == []
    assert await runtime.memory.list() == []


async def test_extraction_validates_evidence_confidence_and_procedural_learning(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def extract(_text: str) -> list[MemoryCandidate]:
        return [
            fact(),
            replace(fact(), text="User likes black tea.", evidence="not in source"),
            replace(fact(), confidence=0.1),
            replace(fact(), kind=MemoryType.PROCEDURAL),
        ]

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    result = await runtime.memory.application.extract(
        "I prefer green tea.", Provenance("user_message", message_id="source")
    )
    assert len(result) == 1 and result[0].text == "User prefers green tea."


async def test_native_management_api_and_idempotent_update(runtime: Runtime, api: httpx.AsyncClient) -> None:
    payload = {"text": "User prefers green tea.", "object": "green tea", "topic": "drink"}
    headers = {"Idempotency-Key": "native-create"}
    queued = await api.post("/api/v1/memories", json=payload, headers=headers)
    assert queued.status_code == 202, queued.text
    assert (await api.post("/api/v1/memories", json=payload, headers=headers)).json()["id"] == queued.json()[
        "id"
    ]
    assert await runtime.memory_tasks.run_once()
    item = (await api.get("/api/v1/memories")).json()["items"][0]
    changed = {**payload, "text": "User now prefers coffee.", "object": "coffee"}
    update = await api.put(f"/api/v1/memories/{item['id']}", json=changed)
    assert update.status_code == 202, update.text
    assert await runtime.memory_tasks.run_once()
    assert (await runtime.operations.get(update.json()["id"])).state == "succeeded"
    history = (await api.get("/api/v1/memories?include_history=true")).json()["items"]
    assert {r["state"] for r in history} == {"active", "superseded"}
    newer = next(r for r in history if r["state"] == "active")
    assert (await api.delete(f"/api/v1/memories/{newer['id']}")).json()["deleted"]
    assert (await api.get("/api/v1/memories")).json()["items"] == []
    assert (await api.post("/api/v1/memories", json={"text": "My password is unsafe"})).status_code == 422


@pytest.mark.parametrize("provider", ["openai", "azure_openai", "ollama"])
async def test_maf_chat_to_native_memory_to_another_session(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
    provider: str,
) -> None:
    from shovel.config.model_settings import ModelProviderType
    from shovel.runtime.agent import create_framework_client

    vectors(runtime, tmp_path)
    runtime.settings.model.model_provider_type = ModelProviderType(provider)
    runtime.settings.model.api_version = "2024-10-21"
    captured: list[dict[str, Any]] = []

    def chat(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Noted."}}]}
        )

    def extract(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert not body.get("tools")
        key = "max_tokens" if provider == "ollama" else "max_completion_tokens"
        assert body[key] == runtime.settings.model.max_output_tokens
        if provider == "azure_openai":
            assert request.headers["api-key"] == runtime.settings.model.security_key
            assert request.url.params["api-version"] == "2024-10-21"
        output = {
            "candidates": [
                {
                    "text": "User prefers green tea.",
                    "kind": "semantic",
                    "subject": "user",
                    "predicate": "prefers",
                    "object": "green tea",
                    "topic": "drink",
                    "confidence": 0.95,
                    "importance": 0.5,
                    "evidence": "I prefer green tea.",
                    "replaces": False,
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(chat))
    extractor = runtime.memory.extractor
    extractor._http = httpx.AsyncClient(transport=httpx.MockTransport(extract))
    extractor._sdk, extractor._framework = create_framework_client(
        runtime.settings.model, extractor._http, max_iterations=1
    )
    first = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{first}/messages", json={"content": "I prefer green tea."}
    )
    assert response.status_code == 200, response.text
    assert await runtime.memory_tasks.run_once()
    while await runtime.memory.maintain():
        pass
    saved = await runtime.memory.list()
    assert len(saved) == 1
    assert saved[0]["provenance"][0]["message_id"] == (await runtime.chat.messages(first))[0].id
    second = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{second}/messages", json={"content": "What drink do I prefer?"}
    )
    assert response.status_code == 200, response.text
    assert "User prefers green tea." in json.dumps(captured[-1], ensure_ascii=False)
    assert "I prefer green tea." not in json.dumps(captured[-1], ensure_ascii=False)
