from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from scripts.memory_baseline import DATASET, Dataset, GoldFact
from scripts.memory_support_evaluation import (
    CASES,
    Answer,
    Question,
    SupportDataset,
    answer_correct,
    prepare_root,
)
from scripts.memory_support_evaluation import main as support_main

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


@pytest.mark.parametrize(
    "query",
    [
        "Which database do I not use for project Solstice?",
        "Which database don't I use for project Solstice?",
        "Solstice 项目没有使用哪个数据库\uff1f",
        "Solstice 项目未使用什么数据库\uff1f",
        "Which database did I use for project Solstice?",
        "Which database was project Solstice using?",
        "Which database did I previously use for project Solstice?",
        "Solstice 项目以前使用什么数据库\uff1f",
        "Solstice 项目原来使用什么数据库\uff1f",
        "Which database do I use for project Solstice in 2024?",
        "Solstice 项目在2024年使用什么数据库\uff1f",
        "Which database do I use and prefer for project Solstice?",
        "Which database and programming language do I use for project Solstice?",
        "Solstice 项目使用什么数据库和编程语言\uff1f",
        "Which database do I use for project Solstice and who leads it?",
        "Solstice 项目使用什么数据库\uff0c谁负责维护\uff1f",
        "Which database do I use for projects Solstice and Tundra?",
        "Solstice 和 Tundra 项目分别使用什么数据库\uff1f",
    ],
)
def test_unverified_qualifiers_do_not_turn_current_facts_into_answer_support(query: str) -> None:
    fact = record(
        text="The user uses SQLite as the database for project Solstice.",
        predicate="uses",
        object="SQLite",
        topic="project.solstice.database",
    )
    assert assess(Query.parse(query), fact, semantic_match=True) == Support.UNKNOWN


@pytest.mark.parametrize(
    "text",
    [
        "The user does not use SQLite for project Solstice.",
        "The user no longer uses SQLite for project Solstice.",
        "Solstice 项目没有使用 SQLite 数据库。",
        "The user previously used SQLite for project Solstice.",
        "The user used SQLite for project Solstice.",
        "Solstice 项目以前使用 SQLite 数据库。",
        "In 2024 the user uses SQLite for project Solstice.",
        "The user uses SQLite but prefers MySQL for project Solstice.",
        "The user uses SQLite for project Solstice and MySQL for project Tundra.",
    ],
)
def test_qualified_or_compound_record_cannot_support_an_unqualified_relation(text: str) -> None:
    fact = record(text=text, predicate="uses", object="SQLite", topic="project.solstice.database")
    assert (
        assess(Query.parse("Which database do I use for project Solstice?"), fact, semantic_match=True)
        == Support.UNKNOWN
    )


@pytest.mark.parametrize(
    "query",
    [
        "Solstice 和 Tundra 项目分别使用什么数据库\uff1f",
        "Solstice、Tundra 项目使用什么数据库\uff1f",
        "Which database do I use for projects Solstice and Tundra?",
        "Which database do the Solstice and Tundra projects use?",
        "Which database do I use for projects Solstice, Tundra and Zephyr?",
    ],
)
def test_coordinated_entities_are_not_silently_reduced_to_one(query: str) -> None:
    expected = {"solstice", "tundra"}
    if "Zephyr" in query:
        expected.add("zephyr")
    assert Query.parse(query).entities == {"project": expected}


@pytest.mark.parametrize(
    "query",
    [
        "Which database do I use for project Solstice?",
        "Solstice 项目使用什么数据库\uff1f",
        "Equinox 项目使用什么数据库\uff1f",
        "Which database do I use for project Equinox?",
    ],
)
def test_single_relation_positive_and_aliases_survive_qualifier_guards(query: str) -> None:
    fact = record(
        text="The user uses SQLite for project Solstice (also known as Equinox).",
        predicate="uses",
        object="SQLite",
        topic="project.solstice.database",
    )
    assert assess(Query.parse(query), fact, semantic_match=True) == Support.SUPPORTED


def test_current_passive_relation_and_product_version_are_not_historical_conditions() -> None:
    fact = record(
        text="SQL Server 2022 is used as the database for project Solstice2024.",
        predicate="uses",
        object="SQL Server 2022",
        topic="project.solstice2024.database",
    )
    assert (
        assess(Query.parse("Which database is used for project Solstice2024?"), fact, semantic_match=True)
        == Support.SUPPORTED
    )


@pytest.mark.parametrize("filename", ["memory_support_cases.json", "memory_support_p0_cases.json"])
def test_existing_support_cases_keep_all_positive_evidence_and_reject_distractors(filename: str) -> None:
    original = Dataset.model_validate_json(DATASET.read_bytes())
    dataset = SupportDataset.model_validate_json(CASES.with_name(filename).read_bytes())
    facts = [
        *(fact for case in original.cases for fact in case.expected),
        *original.distractors,
        *dataset.facts,
    ]
    records = {
        fact.key: replace(
            saved(fact, fact.key),
            topic=fact.topic,
            episode_time_source="user" if fact.occurred_on else None,
        )
        for fact in facts
    }
    for question in dataset.queries:
        supported = {
            key
            for key, fact in records.items()
            if assess(Query.parse(question.text), fact, semantic_match=True) == Support.SUPPORTED
        }
        assert supported == set(question.expected), question.text


def test_explicit_dislike_remains_a_supported_relation() -> None:
    fact = record(text="The user dislikes coffee.", predicate="dislikes", object="coffee", topic="drink")
    assert assess(Query.parse("Which drink do I dislike?"), fact, semantic_match=True) == Support.SUPPORTED
    assert assess(Query.parse("我不喜欢什么饮品\uff1f"), fact, semantic_match=True) == Support.SUPPORTED


def test_profile_qualifiers_are_background_not_historical_or_negative_answers() -> None:
    fact = record(text="My name is Mira.", predicate="has_name", object="Mira")
    assert assess(Query.parse("What is my name?"), fact, semantic_match=True) == Support.SUPPORTED
    for query in ("What was my name previously?", "What is not my name?", "我以前的名字是什么\uff1f"):
        assert assess(Query.parse(query), fact, semantic_match=True) == Support.BACKGROUND


def test_compound_event_does_not_answer_with_only_one_event_direction() -> None:
    fact = record(
        text="Completed the offline evaluation for project Solstice.",
        kind="episodic",
        object="Solstice",
        occurred_on="2026-09-10",
    )
    assert (
        assess(
            Query.parse("When did the Solstice evaluation start and finish?"),
            fact,
            semantic_match=True,
        )
        == Support.UNKNOWN
    )
    assert (
        assess(Query.parse("Solstice 评估何时开始和完成\uff1f"), fact, semantic_match=True) == Support.UNKNOWN
    )
    assert (
        assess(Query.parse("When did the Solstice evaluation finish?"), fact, semantic_match=True)
        == Support.SUPPORTED
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


@pytest.mark.parametrize(
    "query,supported",
    [
        ("Which database do I use for project Larch?", False),
        ("Borealis 项目没有使用哪个数据库\uff1f", False),
        ("Borealis 项目以前使用什么数据库\uff1f", False),
        ("Borealis 和 Larch 项目分别使用什么数据库\uff1f", False),
        ("Which database and programming language do I use for project Borealis?", False),
        ("Which database do I use for project Borealis?", True),
        ("Borealis 项目使用什么数据库\uff1f", True),
    ],
)
async def test_supported_facts_and_background_profile_stay_separate_in_final_request(
    runtime: Runtime, api: httpx.AsyncClient, query: str, supported: bool
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
        assert payloads[0]["memory_status"] == ("supported" if supported else "no_supported_fact")
        evidence = [item for item in payloads[0]["memory"] if item["answer_support"] == "supported"]
        if supported:
            assert [item["object"] for item in evidence] == ["PostgreSQL"]
        else:
            assert evidence == []
            assert "PostgreSQL" not in json.dumps(payloads)
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "No matching recorded fact."}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(chat))
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(
        f"/api/v1/conversations/{identifier}/messages",
        json={"content": query},
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


def test_custom_support_cases_still_require_explicit_live_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SHOVEL_RUN_LIVE_TESTS", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "memory_support_evaluation",
            "--config",
            "never-read.toml",
            "--corpus",
            "unused-corpus",
            "--output",
            "unused-report.json",
            "--cases",
            "never-read-cases.json",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        support_main()
    assert exc.value.code == 2
    assert "SHOVEL_RUN_LIVE_TESTS=1" in capsys.readouterr().err
