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

from shovel.memory.domain import MemoryCandidate, MemoryRecord, MemoryType, Provenance
from shovel.memory.relevance import Query, Support, assess, entities, literal_match
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at
from test_memory_baseline import saved
from test_memory_p0_live import _free_text_answer_correct


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


@pytest.mark.parametrize(
    "instruction",
    [
        "",
        "请只依据已保存的个人记录回答。",
        "请只依据已保存的个人记录回答\uff1b没有记录就明确说明。",
    ],
)
def test_saved_record_response_instructions_do_not_qualify_the_fact(instruction: str) -> None:
    fact = record(
        text="我在青鹭-Q47项目中使用 SQLite 数据库。",
        predicate="uses",
        object="SQLite",
        topic="project.青鹭-q47.database",
    )
    query = Query.parse(f"我在青鹭-Q47项目中使用什么数据库\uff1f{instruction}")
    assert assess(query, fact, semantic_match=True) == Support.SUPPORTED


def test_mixed_chinese_project_names_keep_the_full_identity() -> None:
    assert entities("我在青鹭-Q47项目中使用什么数据库\uff1f") == {"project": {"青鹭-q47"}}
    fact = record(
        text="我在赤狐-Q47项目中使用 PostgreSQL 数据库。",
        predicate="uses",
        object="PostgreSQL",
        topic="project.赤狐-q47.database",
    )
    assert (
        assess(Query.parse("我在青鹭-Q47项目中使用什么数据库\uff1f"), fact, semantic_match=True)
        == Support.CONFLICT
    )


def test_project_context_does_not_replace_the_personal_actor() -> None:
    fact = replace(
        record(
            text="Someone else uses SQLite for project 青鹭-Q47.",
            predicate="uses",
            object="SQLite",
            topic="project.青鹭-q47.database",
        ),
        subject="someone else",
    )
    assert (
        assess(Query.parse("我在青鹭-Q47项目中使用什么数据库\uff1f"), fact, semantic_match=True)
        == Support.RELATED
    )


@pytest.mark.parametrize(
    "question",
    [
        "我在青鹭-Q47项目中没有使用什么数据库\uff1f",
        "我在青鹭-Q47项目中以前使用什么数据库\uff1f",
        "我在青鹭-Q47和赤狐-R82项目中分别使用什么数据库\uff1f",
        "我在青鹭-Q47项目中使用什么数据库和编程语言\uff1f",
    ],
)
def test_response_instructions_never_remove_factual_qualifiers(question: str) -> None:
    fact = record(
        text="我在青鹭-Q47项目中使用 SQLite 数据库。",
        predicate="uses",
        object="SQLite",
        topic="project.青鹭-q47.database",
    )
    query = Query.parse(f"{question}请只依据已保存的个人记录回答\uff1b没有记录就明确说明。")
    assert assess(query, fact, semantic_match=True) == Support.UNKNOWN


async def test_issue_14_saved_fact_reaches_new_session_with_source_evidence(
    runtime: Runtime, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from shovel.memory.domain import MemoryCandidate

    source = "请记住\uff1a我在青鹭-Q47项目中使用 SQLite 数据库。"

    async def extract(text: str) -> list[MemoryCandidate]:
        assert text == source
        return [
            MemoryCandidate(
                "我在青鹭-Q47项目中使用 SQLite 数据库。",
                predicate="uses",
                object="SQLite",
                topic="project.青鹭-q47.database",
                evidence=source,
            )
        ]

    captured: list[dict[str, Any]] = []

    def chat(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        evidence = next(
            json.loads(message["content"].split("\n", 1)[1])
            for message in body["messages"]
            if isinstance(message.get("content"), str)
            and message["content"].startswith("本轮未受信任的参考数据")
        )
        for message in body["messages"]:
            if isinstance(message.get("content"), str) and message["content"].startswith("Host"):
                evidence.update(json.loads(message["content"].split("\n", 1)[1]))
        captured.append(evidence)
        supported = [item for item in evidence.get("memory", []) if item["answer_support"] == "supported"]
        answer = str(supported[0]["object"]) if supported else "没有找到对应的已保存记录。"
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": answer}}]}
        )

    monkeypatch.setattr(runtime.memory.extractor, "extract", extract)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(chat))
    session_a = (await api.post("/api/v1/conversations", json={})).json()["id"]
    response = await api.post(f"/api/v1/conversations/{session_a}/messages", json={"content": source})
    assert response.status_code == 200, response.text
    operation_id = response.json()["memory_operation_id"]
    assert operation_id and await runtime.memory_tasks.run_once()
    assert (await runtime.operations.get(operation_id)).state == "succeeded"
    saved_facts = (await api.get("/api/v1/memories")).json()["items"]
    assert len(saved_facts) == 1
    saved_fact = saved_facts[0]
    assert saved_fact["state"] == "active" and saved_fact["object"] == "SQLite"
    provenance = saved_fact["provenance"][0]
    assert provenance["conversation_id"] == session_a and provenance["evidence"] == source
    history = await runtime.chat.messages(session_a)
    assert provenance["message_id"] == next(message.id for message in history if message.role == "user")
    await runtime.memory.repository.consolidate(
        runtime.memory.owner,
        [
            MemoryCandidate(
                "我在赤狐-Q47项目中使用 PostgreSQL 数据库。",
                predicate="uses",
                object="PostgreSQL",
                topic="project.赤狐-q47.database",
            )
        ],
        Provenance("manual"),
    )
    for project, expected in (("青鹭-Q47", "SQLite"), ("赤狐-Q47", "PostgreSQL"), ("银杉-S96", None)):
        session_b = (await api.post("/api/v1/conversations", json={})).json()["id"]
        question = (
            f"我在{project}项目中使用什么数据库\uff1f请只依据已保存的个人记录回答\uff1b没有记录就明确说明。"
        )
        response = await api.post(f"/api/v1/conversations/{session_b}/messages", json={"content": question})
        assert response.status_code == 200, response.text
        supported = [item for item in captured[-1].get("memory", []) if item["answer_support"] == "supported"]
        assert [item["object"] for item in supported] == ([expected] if expected else [])
        assert captured[-1]["memory_status"] == ("supported" if expected else "no_supported_fact")
        assert response.json()["content"] == (expected or "没有找到对应的已保存记录。")
        if expected == "SQLite":
            assert supported[0]["id"] == saved_fact["id"]
        assert not captured[-1]["sources"]


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


@pytest.mark.parametrize(
    "text,topic,query,expected",
    [
        (
            "Uses SQLite for project Meridian.",
            "project.meridian_database",
            "project Meridian",
            Support.SUPPORTED,
        ),
        ("Uses SQLite for the Meridian project.", "", "project Meridian", Support.SUPPORTED),
        (
            "Uses SQLite for the Meridian repository.",
            "repository.meridian_database",
            "the Meridian repository",
            Support.SUPPORTED,
        ),
        (
            "Uses SQLite for project Meridian_database.",
            "project.meridian_database",
            "project Meridian",
            Support.CONFLICT,
        ),
        ("Uses SQLite.", "project.meridian_database", "project Meridian", Support.CONFLICT),
        ("Uses SQLite for project Meridian.", "project.meridian_custom", "project Meridian", Support.UNKNOWN),
        ("Uses SQLite for project Meridian.", "project.osprey_database", "project Meridian", Support.UNKNOWN),
        (
            "Uses SQLite for project Meridian and project Osprey.",
            "project.meridian_database",
            "project Meridian",
            Support.UNKNOWN,
        ),
    ],
)
def test_topic_attribute_disambiguation_requires_exact_explicit_entity(
    text: str, topic: str, query: str, expected: Support
) -> None:
    fact = record(text=text, predicate="uses", object="SQLite", topic=topic)
    assert assess(Query.parse(f"Which database do I use for {query}?"), fact, semantic_match=True) == expected


def test_suffix_event_names_do_not_relax_event_direction_or_time_evidence() -> None:
    fact = record(
        text="Completed the offline evaluation for the Osprey project.",
        kind="episodic",
        object="Osprey",
        occurred_on="2026-07-14",
    )
    assert (
        assess(Query.parse("When did the Osprey evaluation finish?"), fact, semantic_match=True)
        == Support.SUPPORTED
    )
    assert (
        assess(Query.parse("When did the Osprey evaluation begin?"), fact, semantic_match=True)
        == Support.RELATED
    )
    assert (
        assess(
            Query.parse("When did the Osprey evaluation finish?"),
            replace(fact, episode_time_source="legacy_unspecified"),
            semantic_match=True,
        )
        == Support.UNKNOWN
    )
    assert (
        assess(Query.parse("When did the Meridian evaluation finish?"), fact, semantic_match=True)
        == Support.CONFLICT
    )


@pytest.mark.parametrize(
    "text",
    [
        "The user started the WARMUP-LIVE-36-X project on 2026-10-12.",
        "User started test project WARMUP-LIVE-36-X on 2026-10-12.",
        "用户在2026年10月12日启动了测试项目 WARMUP-LIVE-36-X。",
    ],
)
@pytest.mark.parametrize(
    "question",
    [
        "When did I start the WARMUP-LIVE-36-X project?",
        "When did project WARMUP-LIVE-36-X begin?",
        "When was the WARMUP-LIVE-36-X project started?",
        "我是什么时候启动测试项目 WARMUP-LIVE-36-X 的\uff1f",
        "WARMUP-LIVE-36-X项目是什么时候启动的\uff1f",
    ],
)
def test_project_event_support_survives_language_and_episode_projection(text: str, question: str) -> None:
    fact = replace(
        record(text=text, kind="episodic", object="WARMUP-LIVE-36-X", occurred_on="2026-10-12"),
        subject="",
        predicate="",
        object="",
        topic="",
    )
    assert assess(Query.parse(question), fact, semantic_match=False) == Support.SUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        "我在2026年10月12日开始了紫鸢-V74项目。",
        "用户开始了紫鸢-V74项目。",
        "The user started project 紫鸢-V74 on 2026-10-12.",
    ],
)
@pytest.mark.parametrize(
    "question",
    [
        "我哪一天开始了紫鸢-V74项目\uff1f",
        "我哪天开始了紫鸢-V74项目\uff1f",
        "我什么时候开始了紫鸢-V74项目\uff1f",
        "我何时开始了 紫鸢-V74 项目\uff1f",
        "紫鸢-V74项目何时开始\uff1f",
    ],
)
def test_issue_14_mixed_name_event_recall(text: str, question: str) -> None:
    fact = record(text=text, kind="episodic", object="紫鸢-V74", occurred_on="2026-10-12")
    assert assess(Query.parse(question), fact, semantic_match=False) == Support.SUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        "我在2026年10月12日开始了紫鸢-V74项目。",
        "The user started project 紫鸢-V74 on 2026-10-12.",
    ],
)
async def test_issue_14_keyword_event_recall_without_vectors(runtime: Runtime, text: str) -> None:
    fact = MemoryCandidate(
        text,
        kind=MemoryType.EPISODIC,
        object="紫鸢-V74",
        occurred_at=1791763200,
        time_precision="day",
    )
    saved = await runtime.memory.repository.consolidate(runtime.memory.owner, [fact], Provenance("manual"))
    query = "我哪一天开始了紫鸢-V74项目\uff1f"
    recalled = await runtime.memory.application.retrieve(query, "new-session", use_index=False)
    assert [item["id"] for item in recalled] == [saved[0].id]


@pytest.mark.parametrize(
    "question,changes",
    [
        ("我哪一天完成了紫鸢-V74项目\uff1f", {}),
        ("我哪一天开始了赤狐-V74项目\uff1f", {}),
        ("我哪一天开始了紫鸢-V75项目\uff1f", {}),
        ("我哪一天没有开始紫鸢-V74项目\uff1f", {}),
        ("我哪一天可能开始紫鸢-V74项目\uff1f", {}),
        ("我哪一天计划开始紫鸢-V74项目\uff1f", {}),
        ("紫鸢-V74项目何时开始、何时完成\uff1f请分别说明\uff1b没有记录的部分不要推测。", {}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"text": "其他人开始了紫鸢-V74项目。"}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"text": "用户可能开始了紫鸢-V74项目。"}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"text": "用户没有开始紫鸢-V74项目。"}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"topic": "project.赤狐-v74"}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"occurred_at": None}),
        ("我哪一天开始了紫鸢-V74项目\uff1f", {"episode_time_source": "recorded"}),
    ],
)
def test_issue_14_event_recall_preserves_unknown_and_conflicting_evidence(
    question: str, changes: dict[str, Any]
) -> None:
    fact = record(
        text="用户开始了紫鸢-V74项目。", kind="episodic", object="紫鸢-V74", occurred_on="2026-10-12"
    )
    assert assess(Query.parse(question), replace(fact, **changes), semantic_match=True) != Support.SUPPORTED


@pytest.mark.parametrize(
    "question",
    [
        "When did Alice start the WARMUP-LIVE-36-X project?",
        "When did I plan to start the WARMUP-LIVE-36-X project?",
        "When did I not start the WARMUP-LIVE-36-X project?",
        "When did I start project WARMUP-LIVE-36-X in 2024?",
        "When did I start project WARMUP-LIVE-36-X and finish lunch?",
        "When did I start projects WARMUP-LIVE-36-X and Other?",
        "When did I start project WARMUP-LIVE-36-X and who helped?",
        "When did I start the offline evaluation for project WARMUP-LIVE-36-X?",
        "我什么时候可能启动项目 WARMUP-LIVE-36-X\uff1f",
        "我什么时候启动项目 WARMUP-LIVE-36-X 和 Other\uff1f",
    ],
)
def test_event_questions_do_not_infer_actors_or_unverified_conditions(question: str) -> None:
    fact = record(
        text="The user started project WARMUP-LIVE-36-X on 2026-10-12.",
        kind="episodic",
        object="WARMUP-LIVE-36-X",
        occurred_on="2026-10-12",
    )
    assert assess(Query.parse(question), fact, semantic_match=True) != Support.SUPPORTED


@pytest.mark.parametrize(
    "text",
    [
        "Alice started project WARMUP-LIVE-36-X on 2026-10-12.",
        "The user might start project WARMUP-LIVE-36-X on 2026-10-12.",
        "The user plans to start project WARMUP-LIVE-36-X on 2026-10-12.",
        "用户可能在2026年10月12日启动项目 WARMUP-LIVE-36-X。",
        "用户在2026年10月12日没有启动项目 WARMUP-LIVE-36-X。",
        "The user started the offline evaluation for project WARMUP-LIVE-36-X.",
        "The user met Alice. Alice started project WARMUP-LIVE-36-X on 2026-10-12.",
        "The user started project WARMUP-LIVE-36-X on 2026-10-12 and again on 2026-10-13.",
        "Started project WARMUP-LIVE-36-X on 2026-10-12.",
    ],
)
def test_event_records_require_one_bound_factual_event(text: str) -> None:
    fact = record(text=text, kind="episodic", object="WARMUP-LIVE-36-X", occurred_on="2026-10-12")
    for question in (
        "When did I start project WARMUP-LIVE-36-X?",
        "When did project WARMUP-LIVE-36-X start?",
    ):
        if text.startswith(("Alice", "Started")) and " I " not in question:
            continue
        assert assess(Query.parse(question), fact, semantic_match=True) != Support.SUPPORTED


@pytest.mark.parametrize("change", [{"occurred_at": None}, {"episode_time_source": "recorded"}])
def test_project_event_time_must_remain_explicit(change: dict[str, Any]) -> None:
    fact = record(
        text="The user started project WARMUP-LIVE-36-X.",
        kind="episodic",
        object="WARMUP-LIVE-36-X",
        occurred_on="2026-10-12",
    )
    assert (
        assess(
            Query.parse("When did I start project WARMUP-LIVE-36-X?"),
            replace(fact, **change),
            semantic_match=True,
        )
        == Support.UNKNOWN
    )


@pytest.mark.parametrize(
    "question,changes,reason",
    [
        ("When did this project start?", {}, "unresolved_entity"),
        ("When did I start project Cedar in 2024?", {}, "unverified_conditions"),
        ("When did Alice start project Cedar?", {}, "unsupported_question"),
        ("When did I start project Cedar?", {"text": "Project Cedar started."}, "event_actor"),
        ("When did I start project Cedar?", {"episode_time_precision": "month"}, "event_time"),
        ("When did I start project Birch?", {}, "entity_conflict"),
        ("When did I start project Cedar?", {"text": "The user started a task."}, "entity_missing"),
        ("When did I start project Cedar?", {"topic": "project.birch"}, "entity_binding"),
    ],
)
def test_event_rejection_diagnostics_are_fixed_content_free_codes(
    question: str, changes: dict[str, Any], reason: str
) -> None:
    fact = record(
        text="The user started project Cedar.", kind="episodic", object="Cedar", occurred_on="2026-10-12"
    )
    diagnostics: dict[str, int] = {}
    expected = Support.CONFLICT if reason == "entity_conflict" else Support.UNKNOWN
    assert (
        assess(Query.parse(question), replace(fact, **changes), semantic_match=True, diagnostics=diagnostics)
        == expected
    )
    assert diagnostics == {f"support_rejected_{reason}": 1}


@pytest.mark.parametrize(
    "question,expected",
    [
        ("when did the cedar evaluation finish?", Support.SUPPORTED),
        ("when did the birch evaluation finish?", Support.CONFLICT),
        ("When did the evaluation finish?", Support.UNKNOWN),
        ("When did the offline evaluation finish?", Support.UNKNOWN),
    ],
)
def test_event_target_is_required_even_without_an_explicit_project_category(
    question: str, expected: Support
) -> None:
    fact = record(
        text="The user completed the Cedar evaluation.",
        kind="episodic",
        object="Cedar",
        occurred_on="2026-10-12",
    )
    assert assess(Query.parse(question), fact, semantic_match=True) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("the meridian repository", {"repository": {"meridian"}}),
        ("Meridian team", {"team": {"meridian"}}),
        ("Meridian client", {"customer": {"meridian"}}),
        ("Who leads project Meridian?", {"project": {"meridian"}}),
    ],
)
def test_suffix_names_preserve_entity_kind_without_consuming_prefix_names(
    text: str, expected: dict[str, set[str]]
) -> None:
    assert entities(text) == expected


def test_topic_language_suffix_requires_explicit_name_without_rewriting_literal_name() -> None:
    assert entities("Uses Python for project Meridian.", "project.meridian_language") == {
        "project": {"meridian"}
    }
    assert entities("Uses Python.", "project.meridian_language") == {"project": {"meridian_language"}}


@pytest.mark.parametrize(
    "answer,correct",
    [
        (
            "无法确认项目的数据库。提供资料后\uff0c我可以帮你判断它们使用的是 MySQL、SQLite 等哪种数据库。",
            True,
        ),
        ("I don't know. You use SQLite.", False),
        ("Harbor could refer to several projects; I need a bit more context.", True),
        ("I can't determine which database you use.", True),
        ("Harbor could refer to several projects. You use PostgreSQL.", False),
        ("无法确认。Solstice 项目使用的是 MySQL。", False),
        ("No recorded fact; the database is PostgreSQL.", False),
        ("No date is recorded. The evaluation finished on 2026-08-21.", False),
    ],
)
def test_free_text_abstention_scoring_does_not_confuse_conditional_examples_with_claims(
    answer: str, correct: bool
) -> None:
    assert _free_text_answer_correct(answer, None) == correct


@pytest.mark.parametrize(
    "answer,correct",
    [
        ("Completed on 2026-08-21.", True),
        ("Completed on August 21, 2026.", True),
        ("Completed on 21 August 2026.", True),
        ("完成于2026年8月21日。", True),
        ("完成于 2026 年 8 月 21 日。", True),
        ("Completed on August 22, 2026.", False),
        ("Completed on 2026-08-210.", False),
    ],
)
def test_free_text_event_scoring_accepts_explicit_equivalent_dates(answer: str, correct: bool) -> None:
    assert _free_text_answer_correct(answer, "2026-08-21") == correct


@pytest.mark.parametrize(
    "filename", ["memory_support_cases.json", "memory_support_p0_cases.json", "memory_support_p1_cases.json"]
)
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
    overlapping = {
        "first": GoldFact(key="first", text="First", object="first", aliases=["shared"]),
        "second": GoldFact(key="second", text="Second", object="second", aliases=["shared"]),
    }
    question = Question(text="both", expected=["first", "second"])
    assert not answer_correct(Answer(status="supported", answers=["shared", "wrong"]), question, overlapping)
    assert answer_correct(Answer(status="supported", answers=["shared", "first"]), question, overlapping)


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
