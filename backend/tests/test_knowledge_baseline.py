from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.config.database_config import DatabaseSettings
from shovel.config.settings import AppSettings, load_settings
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.model.runtime import Conversation
from shovel.runtime.schemas import MessageInput, ResourceInput, ScanInput, SearchInput
from shovel.runtime.service import Runtime, create_runtime
from shovel.services.resource_service import ResourceService
from shovel.vector.store import init_vector_store

_FIXTURES = Path(__file__).parent / "fixtures"
_CASES: list[dict[str, Any]] = json.loads((_FIXTURES / "knowledge_cases.json").read_text(encoding="utf-8"))


@asynccontextmanager
async def baseline_runtime(settings: AppSettings) -> AsyncIterator[Runtime]:
    context = create_async_database_engine(settings.database.url)
    engine = await anext(context)
    sessions: async_sessionmaker[AsyncSession] = create_async_session_factory(engine)
    managed = create_runtime(settings, engine, sessions, ResourceService(sessions))
    try:
        yield await anext(managed)
    finally:
        await managed.aclose()
        await context.aclose()


def isolated_settings(root: Path, configured: AppSettings | None = None) -> AppSettings:
    settings = configured or AppSettings()
    return settings.model_copy(
        update={
            "database": DatabaseSettings(url=f"sqlite+aiosqlite:///{(root / 'baseline.db').as_posix()}"),
            "zvec": settings.zvec.model_copy(
                update={
                    "knowledge_path": str(root / "vectors"),
                    "log_dir": str(root / "logs"),
                }
            ),
        }
    )


async def index_corpus(runtime: Runtime, root: Path, *, embed: bool) -> str:
    corpus = root / "corpus"
    shutil.copytree(_FIXTURES / "knowledge", corpus)
    created = await runtime.operations.create_resource(
        ResourceInput(name="baseline", connector_kind="local_fs", config={"root_path": str(corpus)})
    )
    await runtime.operations.run_once()
    assert (await runtime.operations.get(created.id)).state == "succeeded"
    assert created.resource_id is not None
    scan = await runtime.operations.scan(created.resource_id, ScanInput(embed=embed))
    await runtime.operations.run_once()
    assert (await runtime.operations.get(scan.id)).state == "succeeded"
    return created.resource_id


def test_baseline_storage_paths_cannot_be_overridden_by_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHOVEL_DATABASE__URL", "sqlite+aiosqlite:///never-open-user-database.db")
    monkeypatch.setenv("SHOVEL_ZVEC__KNOWLEDGE_PATH", str(tmp_path / "never-open-user-index"))
    settings = isolated_settings(tmp_path)
    assert settings.database.url == f"sqlite+aiosqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    assert settings.zvec.knowledge_path == str(tmp_path / "vectors")


async def test_fixed_chinese_keyword_recall_and_scope_baseline(tmp_path: Path) -> None:
    async with baseline_runtime(isolated_settings(tmp_path)) as runtime:
        resource_id = await index_corpus(runtime, tmp_path, embed=False)
        for case in _CASES:
            result = await runtime.retrieval.search(
                SearchInput(query=case["keyword"], mode="keyword", limit=3)
            )
            assert any(
                hit["doc_uri"].endswith(case["document"]) and hit["resource_id"] == resource_id
                for hit in result["items"]
            ), case
        missing = await runtime.retrieval.search(SearchInput(query="火星基地", mode="keyword"))
        assert missing["items"] == []
        await runtime.operations.resource_action(resource_id, "revoke")
        await runtime.operations.run_once()
        assert (await runtime.retrieval.search(SearchInput(query="离线备份", mode="keyword")))["items"] == []


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1", reason="Explicit live endpoint opt-in required"
)
async def test_live_chinese_retrieval_grounding_and_unknown_answer_baseline(tmp_path: Path) -> None:
    path = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert path, "Set SHOVEL_LIVE_CONFIG to an explicitly selected settings.toml."
    configured = load_settings(path)
    assert configured.model.is_configured, "The live baseline requires a configured chat model."
    assert configured.embedding.is_configured, "The live baseline requires a configured embedding model."
    settings = isolated_settings(tmp_path, configured)
    init_vector_store(settings.zvec)
    report: dict[str, Any] = {"cases": [], "recall_at_3": 0.0, "grounded_accuracy": 0.0}
    async with baseline_runtime(settings) as runtime:
        await index_corpus(runtime, tmp_path, embed=True)
        for case in _CASES:
            result = await runtime.retrieval.search(
                SearchInput(query=case["question"], mode="hybrid", limit=3)
            )
            recalled = any(hit["doc_uri"].endswith(case["document"]) for hit in result["items"])
            async with runtime.sessions() as session:
                conversation = Conversation()
                session.add(conversation)
                await session.commit()
            answer = await runtime.chat.send(
                conversation.id,
                MessageInput(content=case["question"], search_mode="hybrid", memory_enabled=False),
            )
            grounded = all(fact in answer.content for fact in case["facts"]) and any(
                citation["doc_uri"].endswith(case["document"]) for citation in answer.citations
            )
            report["cases"].append(
                {
                    "question": case["question"],
                    "recall": recalled,
                    "grounded": grounded,
                    "answer": answer.content,
                    "sources": [citation["doc_uri"].rsplit("/", 1)[-1] for citation in answer.citations],
                }
            )
        async with runtime.sessions() as session:
            conversation = Conversation()
            session.add(conversation)
            await session.commit()
        unknown = await runtime.chat.send(
            conversation.id,
            MessageInput(
                content="请仅根据本地资料回答:火星基地的预算是多少?如果没有记载请明确说不知道。",
                search_mode="keyword",
                memory_enabled=False,
            ),
        )
        report["unknown_answer"] = unknown.content
        report["refusal"] = bool(
            re.search("没有|不足|无法|未找到|不知道|未提供|未检索|未记载", unknown.content)
        )
    report["recall_at_3"] = sum(row["recall"] for row in report["cases"]) / len(_CASES)
    report["grounded_accuracy"] = sum(row["grounded"] for row in report["cases"]) / len(_CASES)
    output = Path(os.environ.get("SHOVEL_BASELINE_REPORT", str(tmp_path / "baseline-results.json")))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert report["recall_at_3"] == 1.0, report
    assert report["grounded_accuracy"] == 1.0, report
    assert report["refusal"], report
