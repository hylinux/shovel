from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from scripts.memory_baseline import GoldFact
from scripts.memory_support_evaluation import Answer, Question, answer_correct, prepare_root

from shovel.memory.domain import MemoryRecord, Provenance
from shovel.memory.relevance import Query, Support, assess, literal_match
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at
from test_memory_baseline import saved


def record(**values: Any) -> MemoryRecord:
    fact = GoldFact(key="fact", **values)
    return replace(
        saved(fact, "fact"),
        topic=fact.topic,
        episode_time_source="user" if fact.occurred_on else None,
    )


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Which database do I use for project Borealis?", Support.SUPPORTED),
        ("Which database do I use for project Larch?", Support.CONFLICT),
        ("Which airline do I use for business travel?", Support.RELATED),
        ("Who leads project Borealis?", Support.UNKNOWN),
        ("What version of the database does project Borealis use?", Support.UNKNOWN),
        ("Which database do I use for this project?", Support.UNKNOWN),
    ],
)
def test_entity_and_relation_support_is_not_theme_similarity(query: str, expected: Support) -> None:
    fact = record(
        text="The user uses SQLite as the database for project Borealis.",
        predicate="uses",
        object="SQLite",
        topic="project.borealis.database",
    )
    assert assess(Query.parse(query), fact, semantic_match=True) == expected


def test_explicit_aliases_unknown_conditions_and_event_direction() -> None:
    fact = record(
        text="The user uses PostgreSQL for project Borealis (also known as Polaris).",
        predicate="uses",
        object="PostgreSQL",
        topic="project.borealis.database",
    )
    assert (
        assess(Query.parse("Polaris 项目使用什么数据库\uff1f"), fact, semantic_match=True)
        == Support.SUPPORTED
    )
    missing = replace(fact, text="The user uses PostgreSQL.", topic="")
    assert (
        assess(Query.parse("Polaris 项目使用什么数据库\uff1f"), missing, semantic_match=True)
        == Support.UNKNOWN
    )
    episode = record(
        text="Completed the offline evaluation for project Borealis.",
        kind="episodic",
        object="Borealis",
        occurred_on="2026-09-10",
    )
    assert (
        assess(Query.parse("When did the Borealis offline evaluation finish?"), episode, semantic_match=True)
        == Support.SUPPORTED
    )
    assert (
        assess(Query.parse("When did the Borealis offline evaluation begin?"), episode, semantic_match=True)
        == Support.RELATED
    )
    assert (
        assess(Query.parse("When did the Larch offline evaluation finish?"), episode, semantic_match=True)
        == Support.CONFLICT
    )
    assert assess(Query.parse("Borealis评估何时完成"), episode, semantic_match=True) == Support.SUPPORTED
    assert assess(Query.parse("Larch 评估何时完成\uff1f"), episode, semantic_match=True) == Support.CONFLICT
    assert assess(Query.parse("When did I finish lunch?"), episode, semantic_match=True) == Support.UNKNOWN
    assert (
        assess(Query.parse("Which database does Morgan use?"), fact, semantic_match=True) == Support.UNKNOWN
    )
    assert (
        assess(
            Query.parse("When did the Borealis offline evaluation finish?"),
            replace(episode, occurred_at=None),
            semantic_match=True,
        )
        == Support.UNKNOWN
    )
    assert (
        assess(
            Query.parse("When did the Borealis offline evaluation finish?"),
            replace(episode, episode_time_source="legacy_unspecified"),
            semantic_match=True,
        )
        == Support.UNKNOWN
    )


def test_word_boundaries_preserve_mixed_language_identifiers_and_cjk_short_terms() -> None:
    assert literal_match("go", "Go语言")
    assert literal_match("sqlite", "使用SQLite数据库")
    assert literal_match("本地", "本地优先")
    assert not literal_match("go", "Golang and Google")
    assert not literal_match("use", "the user")
    assert not literal_match("sql", "PostgreSQL")


def test_audience_actor_and_topic_constraints_cannot_be_inferred_from_ownership() -> None:
    personal = record(
        text="Uses Obsidian for personal notes.", predicate="uses", object="Obsidian", topic="personal.notes"
    )
    team = record(text="Uses Notion for shared notes.", predicate="uses", object="Notion", topic="team.notes")
    query = Query.parse("Which application do I use for personal notes?")
    assert assess(query, personal, semantic_match=True) == Support.SUPPORTED
    assert assess(query, team, semantic_match=True) == Support.CONFLICT
    assert assess(query, replace(personal, subject="someone else"), semantic_match=True) == Support.RELATED
    missing = replace(personal, text="Uses Obsidian for notes.", topic="notes")
    assert assess(query, missing, semantic_match=True) == Support.UNKNOWN
    inconsistent = record(
        text="Uses SQLite for project Larch.",
        predicate="uses",
        object="SQLite",
        topic="project.borealis.database",
    )
    assert (
        assess(
            Query.parse("Which database do I use for project Borealis?"), inconsistent, semantic_match=True
        )
        == Support.UNKNOWN
    )


async def test_sql_word_boundaries_apply_before_candidate_limit(runtime: Runtime) -> None:
    from shovel.memory.domain import MemoryCandidate

    repo, scope = runtime.memory.repository, runtime.memory.owner
    await repo.consolidate(
        scope,
        [
            MemoryCandidate(f"Uses Google tool {n}.", predicate="uses", object=f"Google {n}")
            for n in range(20)
        ],
        Provenance("manual"),
    )
    exact = (
        await repo.consolidate(
            scope, [MemoryCandidate("Uses Go语言.", predicate="uses", object="Go")], Provenance("manual")
        )
    )[0]
    assert [item.id for item in await repo.keyword(scope, "Go", limit=1)] == [exact.id]
    assert await repo.keyword(scope, "Which tools do I use?", limit=10) == []


async def test_supported_facts_and_background_profile_stay_separate_in_final_request(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    import json

    from shovel.memory.domain import MemoryCandidate

    repo, scope = runtime.memory.repository, runtime.memory.owner
    await repo.consolidate(
        scope,
        [
            MemoryCandidate("My name is Mira.", predicate="has_name", object="Mira"),
            MemoryCandidate(
                "Uses PostgreSQL for project Borealis.",
                predicate="uses",
                object="PostgreSQL",
                topic="project.borealis.database",
            ),
        ],
        Provenance("manual"),
    )

    def chat(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads = [
            json.loads(m["content"].split("\n", 1)[1])
            for m in body["messages"]
            if isinstance(m.get("content"), str) and m["content"].startswith("Host")
        ]
        assert payloads[0]["memory_status"] == "no_supported_fact"
        assert all(item["answer_support"] == "background" for item in payloads[0]["memory"])
        assert "PostgreSQL" not in json.dumps(payloads)
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "No recorded database for Larch."}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(chat))
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{identifier}/messages",
        json={"content": "Which database do I use for project Larch?"},
    )
    assert response.status_code == 200, response.text


def test_answer_scoring_requires_correct_values_and_abstention() -> None:
    fact = GoldFact(key="db", text="Uses SQLite", object="SQLite")
    assert answer_correct(
        Answer(status="supported", answers=["SQLite"]), Question(text="db", expected=["db"]), {"db": fact}
    )
    assert not answer_correct(
        Answer(status="supported", answers=["MySQL"]), Question(text="db", expected=["db"]), {"db": fact}
    )
    assert not answer_correct(
        Answer(status="unknown", answers=[]), Question(text="db", expected=["db"]), {"db": fact}
    )
    assert not answer_correct(
        Answer(status="unknown", answers=["SQLite"]), Question(text="absent", expected=[]), {"db": fact}
    )


def test_frozen_corpus_refuses_existing_unmarked_directories(tmp_path: Any) -> None:
    (tmp_path / "existing").mkdir()
    (tmp_path / "existing" / "unrelated.db").touch()
    with pytest.raises(ValueError, match="new or empty"):
        prepare_root(settings_at(tmp_path), tmp_path / "existing", create=True)
    with pytest.raises(ValueError, match="manifest"):
        prepare_root(settings_at(tmp_path), tmp_path / "existing", create=False)
