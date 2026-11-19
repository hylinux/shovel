from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from shovel.memory.domain import MemoryCandidate, Provenance
from shovel.memory.relevance import Query, Support, assess
from shovel.memory.service import RecallCoverage
from shovel.runtime.service import Runtime
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_memory_relevance import record

QUESTION = "我在青鹭-Q47项目中使用什么数据库\uff1f"
OTHER_QUESTION = "我在赤狐-R82项目中使用什么数据库\uff1f"


@pytest.mark.parametrize("repeat", [2, 3])
def test_identical_questions_have_the_same_support_as_one_question(repeat: int) -> None:
    fact = record(
        text="User uses SQLite for project 青鹭-Q47.",
        predicate="uses",
        object="SQLite",
        topic="project.青鹭-Q47.database",
    )
    single = Query.parse(QUESTION)
    repeated = Query.parse(QUESTION * repeat)
    assert repeated == single
    assert assess(repeated, fact, semantic_match=True) == Support.SUPPORTED


@pytest.mark.parametrize(
    "suffix",
    [
        "我在青鹭-Q47项目中以前使用什么数据库\uff1f",
        "我在青鹭-Q47项目中没有使用什么数据库\uff1f",
        "我在青鹭-Q47项目中使用什么编程语言\uff1f",
        OTHER_QUESTION,
    ],
)
def test_nonidentical_questions_are_not_deduplicated(suffix: str) -> None:
    assert Query.parse(QUESTION + suffix).unverified_conditions


async def save_database(runtime: Runtime, project: str, database: str) -> str:
    saved = await runtime.memory.repository.consolidate(
        runtime.memory.owner,
        [
            MemoryCandidate(
                f"User uses {database} for project {project}.",
                predicate="uses",
                object=database,
                topic=f"project.{project}.database",
            )
        ],
        Provenance("manual"),
    )
    return saved[0].id


@pytest.mark.parametrize("second_saved", [False, True])
async def test_independent_questions_bind_only_their_own_supported_facts(
    runtime: Runtime, second_saved: bool
) -> None:
    first = await save_database(runtime, "青鹭-Q47", "SQLite")
    second = await save_database(runtime, "赤狐-R82", "PostgreSQL") if second_saved else None
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(
        QUESTION + OTHER_QUESTION, use_index=False, coverage=coverage
    )
    assert {item["id"] for item in recalled} == {item for item in (first, second) if item}
    context = coverage.context(recalled)
    assert context["status"] == ("supported" if second_saved else "partial")
    parts = context["parts"]
    assert isinstance(parts, list)
    assert [part["status"] for part in parts] == [
        "supported",
        "supported" if second_saved else "no_match_in_visible_memory",
    ]
    assert [part["facts"] for part in parts] == [
        [{"id": first, "revision": 1}],
        [{"id": second, "revision": 1}] if second else [],
    ]


@pytest.mark.parametrize("qualifier", ["只限以前的记录。", "只限2026年的记录。", "只考虑个人记录。"])
async def test_global_qualifier_is_not_discarded_by_splitting(runtime: Runtime, qualifier: str) -> None:
    await save_database(runtime, "青鹭-Q47", "SQLite")
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(
        QUESTION + OTHER_QUESTION + qualifier, use_index=False, coverage=coverage
    )
    assert recalled == []
    assert coverage.context(recalled)["status"] == "unsupported_query"


@pytest.mark.parametrize(
    "incomplete",
    ["Which database do I use?", "我在这个项目使用什么数据库?", "What is my manager's name?"],
)
async def test_compound_parts_never_inherit_an_implicit_target(runtime: Runtime, incomplete: str) -> None:
    await save_database(runtime, "青鹭-Q47", "SQLite")
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(
        QUESTION + incomplete, use_index=False, coverage=coverage
    )
    assert recalled == []
    assert coverage.context(recalled)["status"] == "unsupported_query"


@pytest.mark.parametrize("count", [4, 5])
async def test_independent_question_count_is_bounded(runtime: Runtime, count: int) -> None:
    first = await save_database(runtime, "青鹭-Q47", "SQLite")
    question = QUESTION + "".join(f"我在项目 Other-{index} 中使用什么数据库?" for index in range(count - 1))
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(question, use_index=False, coverage=coverage)
    if count == 4:
        assert [item["id"] for item in recalled] == [first]
        assert len(coverage.context(recalled)["parts"]) == 4
    else:
        assert recalled == []
        assert coverage.context(recalled)["status"] == "unsupported_query"


async def test_profile_support_in_a_later_part_is_not_downgraded_to_background(runtime: Runtime) -> None:
    first = await save_database(runtime, "青鹭-Q47", "SQLite")
    name = (
        await runtime.memory.repository.consolidate(
            runtime.memory.owner,
            [MemoryCandidate("The user's name is Mira.", predicate="has_name", object="Mira")],
            Provenance("manual"),
        )
    )[0]
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(
        QUESTION + "What is my name?", use_index=False, coverage=coverage
    )
    assert coverage.context(recalled)["status"] == "supported"
    parts = coverage.context(recalled)["parts"]
    assert [part["facts"] for part in parts] == [
        [{"id": first, "revision": 1}],
        [{"id": name.id, "revision": 1}],
    ]


async def test_compound_query_budget_is_shared_and_reports_omitted_parts(runtime: Runtime) -> None:
    await save_database(runtime, "青鹭-Q47", "SQLite")
    await save_database(runtime, "赤狐-R82", "PostgreSQL")
    runtime.memory.application.max_items = 1
    coverage = RecallCoverage()
    recalled = await runtime.memory.application.retrieve(
        QUESTION + OTHER_QUESTION, use_index=False, coverage=coverage
    )
    assert len(recalled) == 1
    parts = coverage.context(recalled)["parts"]
    assert isinstance(parts, list)
    assert sorted(part["status"] for part in parts) == ["omitted_by_budget", "supported"]
    assert all(
        reference["id"] in {item["id"] for item in recalled} for part in parts for reference in part["facts"]
    )


@pytest.mark.parametrize("matching_keyword", [False, True])
async def test_vector_failure_is_explicit_without_discarding_keyword_evidence(
    runtime: Runtime, matching_keyword: bool
) -> None:
    if matching_keyword:
        await save_database(runtime, "青鹭-Q47", "SQLite")
    runtime.memory.application.index = AsyncMock()
    runtime.memory.application.index.search.side_effect = OSError("unavailable test index")
    coverage = RecallCoverage()
    recalled = await runtime.memory.search(QUESTION, coverage=coverage)
    context = coverage.context(recalled)
    assert context["status"] == ("supported" if matching_keyword else "retrieval_unavailable")
    assert context["retrieval"] == "keyword_fallback"


@pytest.mark.parametrize("failure", ["unsupported", "budget", "vector"])
@pytest.mark.parametrize("stream", [False, True])
async def test_memory_only_query_failure_is_explained_without_model_guessing(
    runtime: Runtime, api: httpx.AsyncClient, failure: str, stream: bool
) -> None:
    await save_database(runtime, "青鹭-Q47", "SQLite")
    question = QUESTION
    if failure == "unsupported":
        question = "我在青鹭-Q47项目中以前使用什么数据库\uff1f"
    elif failure == "budget":
        runtime.memory.application.max_chars = 1
    else:
        runtime.memory.application.index = AsyncMock()
        runtime.memory.application.index.search.side_effect = OSError("unavailable test index")
        question = OTHER_QUESTION

    def unexpected_model(request: httpx.Request) -> httpx.Response:
        raise AssertionError("A Memory-only query failure must not be interpreted by the model.")

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_model))
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    if stream:
        response = await api.post(
            "/api/v1/agent",
            json={
                "threadId": identifier,
                "runId": "memory-query-status",
                "messages": [{"id": "question", "role": "user", "content": question}],
                "tools": [],
                "context": [],
                "state": {},
                "forwardedProps": {"knowledge_policy": "disabled", "memory_enabled": True},
            },
        )
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "RUN_FINISHED"
    else:
        response = await api.post(
            f"/api/v1/conversations/{identifier}/messages",
            json={"content": question, "knowledge_policy": "disabled"},
        )
    assert response.status_code == 200, response.text
    history = await runtime.chat.messages(identifier)
    answer = history[-1]
    assert answer.state == "completed" and history[-2].content == question
    assert {"unsupported": "核验", "budget": "预算", "vector": "检索不可用"}[failure] in answer.content
    assert "没有记录" not in answer.content or "不能" in answer.content or "不表示" in answer.content


async def test_repeated_and_compound_questions_reach_final_agent_with_bound_evidence(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    saved_id = await save_database(runtime, "青鹭-Q47", "SQLite")
    captured: dict[str, Any] = {}

    def model(request: httpx.Request) -> httpx.Response:
        for message in json.loads(request.content)["messages"]:
            content = message.get("content")
            if isinstance(content, str) and content.startswith(("Host ", "本轮未受信任的参考数据")):
                captured.update(json.loads(content.split("\n", 1)[1]))
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [{"message": {"role": "assistant", "content": "青鹭: SQLite; 赤狐: 无法确认。"}}],
            },
        )

    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    identifier = (await api.post("/api/v1/conversations", json={})).json()["id"]
    question = QUESTION * 2 + OTHER_QUESTION
    for content in (question, "再查一下记忆。"):
        captured.clear()
        response = await api.post(
            f"/api/v1/conversations/{identifier}/messages",
            json={"content": content, "knowledge_policy": "disabled"},
        )
        assert response.status_code == 200, response.text
        assert captured["memory_query"]["status"] == "partial"
        assert len(captured["memory_query"]["parts"]) == 2
        assert captured["memory_query"]["parts"][0]["facts"] == [{"id": saved_id, "revision": 1}]
        assert captured["memory_query"]["parts"][1]["facts"] == []
        assert [item["id"] for item in captured["memory"] if item["answer_support"] == "supported"] == [
            saved_id
        ]
        history = await runtime.chat.messages(identifier)
        assert history[-2].content == content
