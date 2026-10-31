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
from shovel.db.model.runtime import ChatMessage, Conversation
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


@pytest.mark.parametrize("index_state", ["missing", "weak", "failed"])
async def test_profile_recall_survives_query_variations_and_unavailable_vectors(
    runtime: Runtime, index_state: str
) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    profile = [
        MemoryCandidate("My name is Mike.", predicate="has_name", object="Mike", topic="user.name"),
        MemoryCandidate(
            "I am a senior Java programmer.",
            predicate="has_role",
            object="senior Java programmer",
            topic="user.occupation",
        ),
    ]
    saved = await repo.consolidate(scope, profile, Provenance("manual"))
    await repo.consolidate(scope, [fact()], Provenance("manual"))

    class Index:
        model_version = "unindexed-v1"

        async def search(self, *args: Any) -> list[VectorHit]:
            if index_state == "failed":
                raise RuntimeError("index unavailable")
            return [VectorHit(item.id, item.kind, item.revision, 0.4) for item in saved]

    runtime.memory.application.index = None if index_state == "missing" else Index()
    for query in [
        "\u8fd8\u8bb0\u5f97\u6211\u53eb\u4ec0\u4e48\u5417\uff1f\u6211\u7684\u804c\u4e1a\u662f\u4ec0\u4e48\uff1f",
        "\u4f60\u6ca1\u6709\u8bb0\u4f4f\u6211\u5176\u4ed6\u4f1a\u8bdd\u91cc\u7684\u8bbe\u5b9a\u5417\uff1f",
        "Do you remember my background?",
    ]:
        recalled = await runtime.memory.search(query, conversation_id="new-session")
        assert {entry["id"] for entry in recalled} == {item.id for item in saved}
        assert {entry["object"] for entry in recalled} == {"Mike", "senior Java programmer"}
    if index_state == "failed":
        assert runtime.memory._index_error == "RuntimeError"


async def test_profile_projection_respects_scope_latest_value_expiry_and_deletion(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    name = MemoryCandidate("My name is Mike.", predicate="has_name", object="Mike", topic="user.name")
    own = (await repo.consolidate(scope, [name], Provenance("manual", asserted_at=now_ts() - 60)))[0]
    await repo.consolidate(replace(scope, user_id="other"), [name], Provenance("manual"))
    await repo.consolidate(replace(scope, workspace_id="other"), [name], Provenance("manual"))
    await repo.consolidate(replace(scope, agent_id="other"), [name], Provenance("manual"))
    await repo.consolidate(
        replace(scope, kind=ScopeKind.SESSION, session_id="private"),
        [replace(name, predicate="has_role", object="private role", text="I have a private role.")],
        Provenance("manual"),
    )
    await repo.consolidate(
        scope,
        [replace(name, predicate="has_timezone", object="UTC", expires_at=now_ts() - 1)],
        Provenance("manual"),
    )
    # A large number of newer non-profile facts cannot evict the name.
    await repo.consolidate(
        scope,
        [replace(fact(f"User prefers drink {n}."), object=f"drink {n}") for n in range(110)],
        Provenance("manual"),
    )
    recalled = await runtime.memory.search("background", conversation_id="public")
    assert [entry["id"] for entry in recalled] == [own.id]
    async with runtime.sessions() as session:
        row = await session.get(MemoryBelief, own.id)
        assert row is not None
        row.recorded_at -= 60
        await session.commit()
    newest = (
        await repo.consolidate(
            scope, [replace(name, text="My name is Alex.", object="Alex")], Provenance("manual")
        )
    )[0]
    recalled = await runtime.memory.search("background", conversation_id="public")
    assert [entry["id"] for entry in recalled] == [newest.id]
    assert {
        entry["predicate"] for entry in await runtime.memory.search("background", conversation_id="private")
    } == {"has_name", "has_role"}
    await repo.transition(scope, newest.id, MemoryState.DELETED)
    await repo.transition(scope, own.id, MemoryState.DELETED)
    assert await runtime.memory.search("background", conversation_id="public") == []


async def test_profile_orders_source_assertions_and_reaffirmed_duplicates(runtime: Runtime) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    original = MemoryCandidate("My name is Mike.", predicate="has_name", object="Mike", topic="user.name")
    timestamp = now_ts()
    mike = (await repo.consolidate(scope, [original], Provenance("manual", asserted_at=timestamp - 60)))[0]
    alex = (
        await repo.consolidate(
            scope,
            [replace(original, text="My name is Alex.", object="Alex")],
            Provenance("manual", asserted_at=timestamp - 120),
        )
    )[0]
    assert [item.id for item in await repo.profile(scope, limit=4)] == [mike.id]
    reaffirmed = await repo.consolidate(
        scope,
        [replace(original, text="My name is Alex.", object="Alex")],
        Provenance("manual", asserted_at=timestamp),
    )
    assert reaffirmed[0].id == alex.id
    assert [item.id for item in await repo.profile(scope, limit=4)] == [alex.id]


async def test_old_session_reads_newer_profile_but_pending_assertions_keep_history(runtime: Runtime) -> None:
    from agent_framework import Message

    from shovel.runtime.schemas import MessageInput

    timestamp = now_ts() - 120
    source = "My name is Mike and I am a senior Java programmer."
    async with runtime.sessions() as session:
        conversation = Conversation()
        session.add(conversation)
        await session.flush()
        identifier = conversation.id
        session.add(
            ChatMessage(
                conversation_id=identifier,
                role="user",
                content=source,
                sequence=0,
                created_at=timestamp,
            )
        )
        await session.commit()
    repo, scope = runtime.memory.repository, runtime.memory.owner
    await repo.consolidate(
        scope,
        [
            MemoryCandidate("My name is Alex.", predicate="has_name", object="Alex", topic="user.name"),
            MemoryCandidate(
                "I am a teacher.", predicate="has_role", object="teacher", topic="user.occupation"
            ),
        ],
        Provenance("user_message", conversation_id="other-session", asserted_at=timestamp + 60),
    )
    request = MessageInput(content="What is my name and what is my occupation?")
    history = [Message("user", source)]
    result = await runtime.chat.agent.profile_response(identifier, request, history, None)
    assert result is not None and "Alex" in result.content and "teacher" in result.content
    pending = "My name is Jordan and I am an accountant."
    async with runtime.sessions() as session:
        session.add(
            ChatMessage(
                conversation_id=identifier,
                role="user",
                content=pending,
                sequence=1,
                created_at=timestamp + 100,
            )
        )
        await session.commit()
    history.append(Message("user", pending))
    assert await runtime.chat.agent.profile_response(identifier, request, history, None) is None


async def test_extraction_keeps_short_profile_assertions_semantic(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "\u6211\u53ebMike\uff0c\u6211\u662fJava\u7a0b\u5e8f\u5458"

    async def extract(_text: str) -> list[MemoryCandidate]:
        return [
            MemoryCandidate(
                "\u6211\u53ebMike",
                kind=MemoryType.EPISODIC,
                predicate="has_name",
                object="Mike",
                topic="user.name",
                evidence="\u6211\u53ebMike",
            ),
            MemoryCandidate(
                "\u6211\u662fJava\u7a0b\u5e8f\u5458",
                kind=MemoryType.EPISODIC,
                predicate="has_role",
                object="Java programmer",
                topic="user.occupation",
                evidence="\u6211\u662fJava\u7a0b\u5e8f\u5458",
            ),
        ]

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    saved = await runtime.memory.application.extract(source, Provenance("user_message"))
    assert len(saved) == 2
    assert all(item.kind == MemoryType.SEMANTIC for item in saved)
    assert {entry["predicate"] for entry in await runtime.memory.search("background")} == {
        "has_name",
        "has_role",
    }


async def test_shared_evidence_text_does_not_merge_distinct_profile_assertions(runtime: Runtime) -> None:
    source = "My name is Mike and I am a senior Java programmer."
    candidates = [
        MemoryCandidate(source, predicate="has_name", object="Mike", topic="user.name"),
        MemoryCandidate(
            source, predicate="has_role", object="senior Java programmer", topic="user.occupation"
        ),
    ]
    saved = await runtime.memory.repository.consolidate(
        runtime.memory.owner, candidates, Provenance("manual")
    )
    assert len({item.id for item in saved}) == 2
    assert {
        item.predicate for item in await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
    } == {"has_name", "has_role"}
    duplicate = await runtime.memory.repository.consolidate(
        runtime.memory.owner, candidates, Provenance("manual")
    )
    assert {item.id for item in duplicate} == {item.id for item in saved}


async def test_invented_correction_flag_does_not_drop_fact_or_supersede_previous(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = MemoryCandidate("My name is Alex.", predicate="has_name", object="Alex", topic="user.name")
    old = (
        await runtime.memory.repository.consolidate(runtime.memory.owner, [original], Provenance("manual"))
    )[0]
    source = "Remember my name is Mike."

    async def extract(_text: str, _related: Any = ()) -> list[MemoryCandidate]:
        return [replace(original, text=source, object="Mike", evidence=source, replaces=True)]

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    saved = await runtime.memory.application.extract(source, Provenance("user_message"))
    assert len(saved) == 1 and saved[0].object == "Mike"
    assert (await runtime.memory.repository.get(runtime.memory.owner, old.id)).state == MemoryState.ACTIVE


@pytest.mark.parametrize(
    "predicate,previous,current",
    [
        ("has_name", "Alex", "Mike"),
        ("has_role", "software engineer", "senior Java programmer"),
    ],
)
async def test_profile_merge_cannot_discard_new_attribute_values(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    predicate: str,
    previous: str,
    current: str,
) -> None:
    repo, scope = runtime.memory.repository, runtime.memory.owner
    original = MemoryCandidate(previous, predicate=predicate, object=previous)
    old = (await repo.consolidate(scope, [original], Provenance("manual", asserted_at=now_ts() - 60)))[0]
    source = f"Remember my {'name' if predicate == 'has_name' else 'occupation'} is {current}."

    async def extract(_source: str, _related: Any) -> list[MemoryCandidate]:
        return [replace(original, text=source, object=current, evidence=source, merge_with=old.id)]

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    saved = await runtime.memory.application.extract(source, Provenance("user_message"), related=[old])
    assert len(saved) == 1 and saved[0].id != old.id and saved[0].object == current
    assert (await repo.get(scope, old.id)).object == previous
    assert [item.object for item in await repo.profile(scope, limit=4)] == [current]


@pytest.mark.parametrize(
    "query,scoped,automatic",
    [
        (
            "\u8fd8\u8bb0\u5f97\u6211\u53eb\u4ec0\u4e48\uff1f\u6211\u7684\u804c\u4e1a\u662f\u4ec0\u4e48\u5417\uff1f",
            False,
            False,
        ),
        (
            "\u4f60\u6ca1\u6709\u8bb0\u4f4f\u6211\u5176\u4ed6\u4f1a\u8bdd\u91cc\u7684\u8bbe\u5b9a\u5417\uff1f",
            False,
            False,
        ),
        ("What is my name and what is my occupation?", False, False),
        ("What is my name?", True, True),
        ("My name is Alex. What is my occupation?", False, True),
        (
            "\u6211\u559c\u6b22Rust\uff0c\u8fd8\u8bb0\u5f97\u6211\u53eb\u4ec0\u4e48\u5417\uff1f",
            False,
            True,
        ),
        ("According to the documents, what is my occupation?", False, True),
    ],
)
async def test_personal_memory_questions_route_reads_without_disabling_agent_tools(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, query: str, scoped: bool, automatic: bool
) -> None:
    from shovel.runtime.schemas import MessageInput

    searches: list[object] = []
    model_calls: list[object] = []

    async def search(request: object) -> dict[str, Any]:
        searches.append(request)
        return {"items": []}

    def answer(request: httpx.Request) -> httpx.Response:
        model_calls.append(request)
        assert json.loads(request.content).get("tools")
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Noted."}}]}
        )

    monkeypatch.setattr(runtime.retrieval, "search", search)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    result = await runtime.chat.agent.respond(
        "new-session", MessageInput(content=query, resource_ids=["archive"] if scoped else []), []
    )
    assert bool(searches) == automatic
    assert bool(model_calls) == automatic
    assert result.extract_long_term == automatic


async def test_real_extractor_rejects_paraphrased_evidence_before_partial_commit(runtime: Runtime) -> None:
    from shovel.runtime.agent import create_framework_client

    source = "My name is Mike. I am a senior Java software programmer."

    def extract(_request: httpx.Request) -> httpx.Response:
        candidates = [
            {
                "text": source,
                "kind": "semantic",
                "predicate": predicate,
                "object": value,
                "topic": topic,
                "evidence": evidence,
                "confidence": 1,
                "importance": 1,
            }
            for predicate, value, topic, evidence in [
                ("has_name", "Mike", "user.name", "My name is Mike."),
                ("has_role", "Java programmer", "user.occupation", "I am a senior Java programmer."),
            ]
        ]
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps({"candidates": candidates})}}
                ],
            },
        )

    extractor = runtime.memory.extractor
    extractor._http = httpx.AsyncClient(transport=httpx.MockTransport(extract))
    extractor._sdk, extractor._framework = create_framework_client(
        runtime.settings.model, extractor._http, max_iterations=1
    )
    with pytest.raises(ValueError, match="evidence not present"):
        await runtime.memory.application.extract(source, Provenance("user_message"))
    assert await runtime.memory.list() == []


async def test_profile_lookup_streams_and_persists_without_model_guessing(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    candidates = [
        MemoryCandidate("My name is Mike.", predicate="has_name", object="Mike", topic="user.name"),
        MemoryCandidate(
            "I am a senior Java programmer.",
            predicate="has_role",
            object="senior Java programmer",
            topic="user.occupation",
        ),
    ]
    await runtime.memory.repository.consolidate(runtime.memory.owner, candidates, Provenance("manual"))

    def forbidden(_request: httpx.Request) -> httpx.Response:
        pytest.fail("A committed profile lookup must not ask a model to guess the facts.")

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    for _ in range(2):
        identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
        response = await api.post(
            "/api/v1/agent",
            json={
                "threadId": identifier,
                "runId": identifier,
                "messages": [
                    {
                        "id": "question",
                        "role": "user",
                        "content": (
                            "\u8fd8\u8bb0\u5f97\u6211\u53eb\u4ec0\u4e48\uff1f"
                            "\u6211\u7684\u804c\u4e1a\u662f\u4ec0\u4e48\u5417\uff1f"
                        ),
                    }
                ],
                "forwardedProps": {},
                "context": [],
                "tools": [],
            },
        )
        assert response.status_code == 200, response.text
        assert all(
            event in response.text
            for event in ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED"]
        )
        assert "Mike" in response.text and "Java" in response.text
        saved = (await runtime.chat.messages(identifier))[-1]
        assert saved.state == "completed"
        assert "Mike" in saved.content and "senior Java programmer" in saved.content
        assert saved.memory_operation_id is None


async def test_profile_lookup_preserves_recent_assertions_until_their_commit(runtime: Runtime) -> None:
    from agent_framework import Message

    from shovel.runtime.schemas import MessageInput

    repo, scope = runtime.memory.repository, runtime.memory.owner
    candidates = [
        MemoryCandidate("My name is Alex.", predicate="has_name", object="Alex", topic="user.name"),
        MemoryCandidate("I am a teacher.", predicate="has_role", object="teacher", topic="user.occupation"),
    ]
    await repo.consolidate(scope, candidates, Provenance("manual", asserted_at=now_ts() - 60))
    source = "My name is Mike and I am a senior Java programmer."
    calls: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body.get("tools")
        assert source in json.dumps(body["messages"])
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "Mike, senior Java programmer."}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    history = [Message("user", source), Message("assistant", "Received.")]
    request = MessageInput(content="What is my name and what is my occupation?")
    result = await runtime.chat.agent.respond("current-session", request, history)
    assert result.content == "Mike, senior Java programmer." and len(calls) == 1
    await repo.consolidate(
        scope,
        [
            replace(candidates[0], text=source, object="Mike", evidence=source),
            replace(candidates[1], text=source, object="senior Java programmer", evidence=source),
        ],
        Provenance("user_message", conversation_id="current-session"),
    )
    result = await runtime.chat.agent.respond("current-session", request, history)
    assert "Mike" in result.content and "senior Java programmer" in result.content
    assert len(calls) == 1


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
@pytest.mark.parametrize(
    "source,candidates,query",
    [
        (
            "I prefer green tea.",
            [
                {
                    "text": "User prefers green tea.",
                    "predicate": "prefers",
                    "object": "green tea",
                    "topic": "drink",
                    "evidence": "I prefer green tea.",
                }
            ],
            "What drink do I prefer?",
        ),
        (
            "Remember that my name is Mike and I am a senior Java programmer.",
            [
                {
                    "text": "\u7528\u6237\u53ebMike",
                    "predicate": "has_name",
                    "object": "Mike",
                    "topic": "user.name",
                    "evidence": "my name is Mike",
                },
                {
                    "text": "The user is a senior Java programmer.",
                    "kind": "episodic",
                    "predicate": "has_role",
                    "object": "senior Java programmer",
                    "topic": "user.occupation",
                    "evidence": "I am a senior Java programmer",
                },
            ],
            "What is my name and what is my occupation?",
        ),
        (
            "\u8bb0\u4f4f\u6211\u7684\u540d\u5b57\u53ebMike, "
            "\u6211\u662f\u4e00\u4f4d\u8d44\u6df1\u7684Java\u7a0b\u5e8f\u5458",
            [
                {
                    "text": "The user's name is Mike.",
                    "predicate": "has_name",
                    "object": "Mike",
                    "topic": "user.name",
                    "evidence": "\u6211\u7684\u540d\u5b57\u53ebMike",
                },
                {
                    "text": "The user is a senior Java programmer.",
                    "predicate": "has_role",
                    "object": "senior Java programmer",
                    "topic": "user.occupation",
                    "evidence": "\u6211\u662f\u4e00\u4f4d\u8d44\u6df1\u7684Java\u7a0b\u5e8f\u5458",
                },
            ],
            "\u6211\u7684\u540d\u5b57\u53eb\u4ec0\u4e48\uff0c\u6211\u7684\u804c\u4e1a\u662f\u4ec0\u4e48\uff1f",
        ),
    ],
)
async def test_maf_chat_to_native_memory_to_another_session(
    runtime: Runtime,
    api: httpx.AsyncClient,
    tmp_path: Path,
    provider: str,
    source: str,
    candidates: list[dict[str, str]],
    query: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shovel.config.model_settings import ModelProviderType
    from shovel.runtime.agent import create_framework_client

    vectors(runtime, tmp_path)
    runtime.settings.model.model_provider_type = ModelProviderType(provider)
    runtime.settings.model.api_version = "2024-10-21"
    captured: list[dict[str, Any]] = []

    if candidates[0]["predicate"] == "has_name":
        runtime.settings.model.max_input_chars = 12000

        async def noisy_documents(_request: Any) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "chunk_id": f"noise-{n}",
                        "resource_id": "archive",
                        "resource_name": "Jordan's documents",
                        "doc_uri": r"C:\Users\Jordan\archive.txt",
                        "title": "Another person's account inventory",
                        "text": "Jordan is an accountant. This archive is not the user's profile. " * 60,
                        "score": 0.9,
                    }
                    for n in range(6)
                ]
            }

        monkeypatch.setattr(runtime.retrieval, "search", noisy_documents)

    def chat(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        answer = "Noted."
        if body["messages"][-1]["content"] != source:
            evidence = json.loads(body["messages"][-2]["content"].split("\n", 1)[1])
            assert body.get("tools")
            assert {entry["memory"] for entry in evidence["memory"]} == {
                candidate["text"] for candidate in candidates
            }
            if candidates[0]["predicate"] == "has_name":
                documents = json.loads(body["messages"][-3]["content"].split("\n", 1)[1])
                assert documents["sources"]
                assert {entry["predicate"] for entry in evidence["memory"]} == {"has_name", "has_role"}
            assert body["messages"][-2]["role"] == "user"
            assert body["messages"][-2]["content"].startswith("Host")
            answer = " / ".join(candidate["object"] for candidate in candidates)
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": answer}}]}
        )

    def extract(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert not body.get("tools")
        allowed_evidence = body["response_format"]["json_schema"]["schema"]["$defs"]["CandidateOutput"][
            "properties"
        ]["evidence"]["enum"]
        assert source in allowed_evidence
        assert all(evidence in source for evidence in allowed_evidence)
        key = "max_tokens" if provider == "ollama" else "max_completion_tokens"
        assert body[key] == runtime.settings.model.max_output_tokens
        if provider == "azure_openai":
            assert request.headers["api-key"] == runtime.settings.model.security_key
            assert request.url.params["api-version"] == "2024-10-21"
        output = {
            "candidates": [
                {
                    "kind": "semantic",
                    "subject": "user",
                    "confidence": 0.95,
                    "importance": 0.5,
                    "replaces": False,
                    **candidate,
                }
                for candidate in candidates
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
    response = await api.post(f"/api/v1/conversations/{first}/messages", json={"content": source})
    assert response.status_code == 200, response.text
    operation_id = response.json()["memory_operation_id"]
    assert operation_id
    assert await runtime.memory_tasks.run_once()
    operation = await runtime.operations.get(operation_id)
    assert operation.state == "succeeded", operation.error
    while await runtime.memory.maintain():
        pass
    saved = await runtime.memory.list()
    assert len(saved) == len(candidates)
    assert {item["memory"] for item in saved} == {item["text"] for item in candidates}
    source_message_id = (await runtime.chat.messages(first))[0].id
    assert all(
        item["scope"] == "agent" and item["provenance"][0]["message_id"] == source_message_id
        for item in saved
    )
    recall_input: dict[str, Any] = {"content": query}
    if candidates[0]["predicate"] == "has_name":
        recall_input["resource_ids"] = ["archive"]
    same_session = await api.post(f"/api/v1/conversations/{first}/messages", json=recall_input)
    assert same_session.status_code == 200, same_session.text
    assert all(candidate["object"] in same_session.json()["content"] for candidate in candidates)
    assert runtime.memory.index is not None
    await runtime.memory.index.close()
    vectors(runtime, tmp_path)
    second = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(f"/api/v1/conversations/{second}/messages", json=recall_input)
    assert response.status_code == 200, response.text
    assert all(candidate["object"] in response.json()["content"] for candidate in candidates)
    context = json.dumps(captured[-1], ensure_ascii=False)
    assert all(candidate["text"] in context for candidate in candidates)
    assert source not in context


async def test_name_management_and_correction_preserve_other_facts(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    payload = {
        "text": "The user's name is Mike.",
        "predicate": "has_name",
        "object": "Mike",
        "topic": "user.name",
    }
    created = await api.post("/api/v1/memories", json=payload)
    assert created.status_code == 202, created.text
    assert await runtime.memory_tasks.run_once()
    assert (await runtime.operations.get(created.json()["id"])).state == "succeeded"
    original = (await api.get("/api/v1/memories")).json()["items"][0]
    role = (
        await runtime.memory.repository.consolidate(
            runtime.memory.owner,
            [
                MemoryCandidate(
                    "The user is a senior Java programmer.",
                    predicate="has_role",
                    object="senior Java programmer",
                    topic="user.occupation",
                )
            ],
            Provenance("manual"),
        )
    )[0]
    updated = await api.put(
        f"/api/v1/memories/{original['id']}",
        json={**payload, "text": "The user's name is Alex.", "object": "Alex"},
    )
    assert updated.status_code == 202, updated.text
    assert await runtime.memory_tasks.run_once()
    assert (await runtime.operations.get(updated.json()["id"])).state == "succeeded"
    active = await runtime.memory.repository.list_records(runtime.memory.owner)
    assert {item.object for item in active} == {"Alex", role.object}
    assert (
        await runtime.memory.repository.get(runtime.memory.owner, original["id"])
    ).state == MemoryState.SUPERSEDED
