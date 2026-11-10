from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from scripts.memory_baseline import baseline_runtime, isolated_settings
from scripts.memory_support_evaluation import AnswerTransport, source_fingerprints

from shovel.config.settings import AppSettings
from shovel.db.model.runtime import Operation
from shovel.memory.domain import MemoryCandidate, MemoryState, Provenance
from shovel.runtime.api import configure_api
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.service import Runtime
from test_hosted_runtime import settings_at
from test_memory_p0_live import _free_text_answer_correct
from test_native_memory import vectors


def _references(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        json.loads(message["content"].split("\n", 1)[1])
        for message in body["messages"]
        if isinstance(message.get("content"), str)
        and message["content"].startswith(
            ("Host ", "本轮未受信任的参考数据", "Current General settings profile")
        )
    ]


async def _extract(source: str, *_args: Any) -> list[MemoryCandidate]:
    if not source.startswith(("I prefer", "My name is")):
        return []
    drink = "jasmine tea" if "jasmine" in source else "green tea"
    result = [MemoryCandidate(f"User prefers {drink}.", object=drink, topic="drink", evidence=source)]
    if "Juniper" in source:
        result.append(
            MemoryCandidate(
                "The user's name is Juniper.",
                predicate="has_name",
                object="Juniper",
                topic="user.name",
                evidence=source,
            )
        )
    return result


@asynccontextmanager
async def _running(
    settings: AppSettings,
    root: Path,
    live: bool,
    requests: list[list[dict[str, Any]]],
) -> AsyncIterator[tuple[Runtime, httpx.AsyncClient]]:
    async with baseline_runtime(settings) as runtime:
        if not live:
            vectors(runtime, root)
            runtime.memory.extractor.extract = AsyncMock(side_effect=_extract)

        async def capture(request: httpx.Request) -> None:
            requests.append(_references(json.loads(await request.aread())))

        def model(request: httpx.Request) -> httpx.Response:
            supported = [
                item["object"]
                for payload in _references(json.loads(request.content))
                for item in payload.get("memory", [])
                if item.get("answer_support") == "supported"
            ]
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": " / ".join(supported) if supported else "UNKNOWN",
                            }
                        }
                    ],
                },
            )

        runtime.chat.agent._client = httpx.AsyncClient(
            transport=AnswerTransport() if live else httpx.MockTransport(model),
            event_hooks={"request": [capture]},
        )
        app = FastAPI()
        configure_api(app, lambda: runtime, settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:9999"
        ) as api:
            yield runtime, api


async def _send(api: httpx.AsyncClient, source: str) -> dict[str, Any]:
    created = await api.post("/api/v1/conversations", json={})
    assert created.status_code == 201, created.text
    response = await api.post(
        f"/api/v1/conversations/{created.json()['id']}/messages", json={"content": source}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "completed"
    return response.json()


async def _drain(runtime: Runtime) -> None:
    for _ in range(10):
        if not await runtime.memory.maintain():
            break
    else:
        pytest.fail("Memory index maintenance did not settle.")
    status = await runtime.memory.status()
    assert status["index_state"] == "ready", status
    assert status["pending_index_operations"] == 0


async def _read(
    runtime: Runtime,
    api: httpx.AsyncClient,
    requests: list[list[dict[str, Any]]],
    expected: str | None,
) -> dict[str, Any]:
    before = len(requests)
    prompt = "Which drink do I prefer?"
    if expected is None:
        prompt += " Use only stored Memory; if no fact is supported, say UNKNOWN."
    answer = await _send(api, prompt)
    assert len(requests) > before, "Must exercise the model in a new Session, not a Profile shortcut."
    payloads = requests[-1]
    supported = [
        item
        for payload in payloads
        for item in payload.get("memory", [])
        if item.get("answer_support") == "supported"
    ]
    assert {item["object"].casefold() for item in supported} == ({expected.casefold()} if expected else set())
    statuses = [payload["memory_status"] for payload in payloads if "memory_status" in payload]
    assert statuses and set(statuses) == {"supported" if expected else "no_supported_fact"}
    assert _free_text_answer_correct(
        answer["content"].casefold(), expected.casefold() if expected else None
    ), answer["content"]
    if answer["memory_operation_id"]:
        for _ in range(10):
            operation = await runtime.operations.get(answer["memory_operation_id"])
            if operation.state == "succeeded":
                assert operation.result["memories"] == 0, "A question must not learn from recalled facts."
                break
            assert operation.state == "queued", operation.error
            assert await runtime.memory_tasks.run_once()
        else:
            pytest.fail("The question's extraction receipt did not settle.")
    return {"answer": answer["content"], "references": payloads}


def _fingerprints() -> dict[str, str]:
    root = Path(__file__).parents[1]
    paths = [
        root / "src" / "shovel" / "runtime" / name
        for name in ("memory.py", "memory_tasks.py", "chat.py", "service.py", "api.py")
    ]
    paths.append(root / "src" / "shovel" / "memory" / "zvec_index.py")
    paths.append(root / "tests" / "test_memory_p0_live.py")
    return {
        **source_fingerprints(),
        **{
            str(path.relative_to(root.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
        str(Path(__file__).relative_to(root.parent)): hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _save(root: Path, live: bool, name: str, report: dict[str, Any]) -> None:
    directory = Path(os.environ.get("SHOVEL_P2_LIVE_REPORT_DIR", str(root))) if live else root
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _report(settings: AppSettings, live: bool, sources: dict[str, str]) -> dict[str, Any]:
    extraction = settings.memory.llm if settings.memory.llm.is_configured else settings.model
    return {
        "suite_version": "memory-lifecycle-p2-v1",
        "live": live,
        "complete": False,
        "source_sha256": sources,
        "answer_model": settings.model.model_name,
        "extraction_model": extraction.model_name,
        "memory": settings.memory.model_dump(
            include={"dim", "max_items", "context_max_chars", "min_similarity", "review_required"}
        ),
    }


@asynccontextmanager
async def _paused_worker(
    runtime: Runtime,
) -> AsyncIterator[tuple[asyncio.Event, asyncio.Task[bool], list[MemoryCandidate]]]:
    ready, release = asyncio.Event(), asyncio.Event()
    original = runtime.memory.extractor.extract
    observed: list[MemoryCandidate] = []

    async def delayed(source: str, *args: Any) -> list[MemoryCandidate]:
        observed.extend(await original(source, *args))
        ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A provider completion can arrive after caller cancellation.
            await release.wait()
        return observed

    runtime.memory.extractor.extract = delayed
    worker = asyncio.create_task(runtime.memory_tasks.run_once())
    waiting = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait((worker, waiting), timeout=330, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
            pytest.fail("Extraction completed before reaching the commit race.")
        assert waiting in done, "Extraction provider did not reach the commit race."
        yield release, worker, observed
    finally:
        release.set()
        waiting.cancel()
        await asyncio.gather(waiting, worker, return_exceptions=True)
        runtime.memory.extractor.extract = original


async def _lifecycle(root: Path, settings: AppSettings, *, live: bool) -> None:
    requests: list[list[dict[str, Any]]] = []
    sources = _fingerprints()
    report = _report(settings, live, sources)
    stages: dict[str, Any] = {}
    report["stages"] = stages
    try:
        async with _running(settings, root, live, requests) as (runtime, api):
            assert runtime.memory.index is not None
            report["embedding_model_version"] = runtime.memory.index.model_version
            answer = await _send(api, "I prefer green tea.")
            identifier = answer["memory_operation_id"]
            assert identifier and (await runtime.operations.get(identifier)).state == "queued"
            assert await runtime.memory.list() == []
            async with runtime.sessions() as session:
                operation = await session.get(Operation, identifier)
                assert operation is not None
                operation.state = "running"
                await session.commit()
            stages["answer_committed_extraction_uncommitted"] = {"operation_id": identifier}

        async with _running(settings, root, live, requests) as (runtime, api):
            assert (await runtime.operations.get(identifier)).state == "queued"
            assert await runtime.memory_tasks.run_once()
            receipt = await runtime.operations.get(identifier)
            assert receipt.state == "succeeded" and receipt.result["committed"]
            records = await runtime.memory.list()
            assert len(records) == 1 and records[0]["object"].casefold() == "green tea"
            old_id = records[0]["id"]
            assert (await runtime.memory.status())["pending_index_operations"] > 0
            await _drain(runtime)
            stages["recovered_extract_cross_session"] = await _read(runtime, api, requests, "green tea")
            update = await api.put(
                f"/api/v1/memories/{old_id}",
                json={"text": "User now prefers mint tea.", "object": "mint tea", "topic": "drink"},
                headers={"Idempotency-Key": "p2-correction"},
            )
            assert update.status_code == 202, update.text
            update_id = update.json()["id"]
            async with runtime.sessions() as session:
                operation = await session.get(Operation, update_id)
                assert operation is not None
                operation.state = "running"
                await session.commit()

        async with _running(settings, root, live, requests) as (runtime, api):
            assert (await runtime.operations.get(update_id)).state == "queued"
            assert await runtime.memory_tasks.run_once()
            assert (await runtime.operations.get(update_id)).state == "succeeded"
            assert (await runtime.memory.owned(old_id)).state == MemoryState.SUPERSEDED
            assert runtime.memory.index is not None
            stale = await runtime.memory.index.search("tea", runtime.memory.owner, 10)
            assert old_id in {hit.id for hit in stale}, "The correction race must contain the old vector."
            stages["corrected_before_index_catches_up"] = await _read(runtime, api, requests, "mint tea")
            stages["corrected_before_index_catches_up"]["rejected_stale_id"] = old_id
            await _drain(runtime)
            current = await runtime.memory.list()
            assert len(current) == 1 and current[0]["object"] == "mint tea"
            current_id = current[0]["id"]
            assert runtime.memory.index is not None
            hits = await runtime.memory.index.search("Which drink do I prefer?", runtime.memory.owner, 10)
            assert old_id not in {hit.id for hit in hits}
            assert current_id in {hit.id for hit in hits}
            late = await _send(api, "I prefer jasmine tea.")
            late_id = late["memory_operation_id"]
            assert late_id and (await runtime.operations.get(late_id)).state == "queued"
            async with _paused_worker(runtime) as (release, worker, _observed):
                assert (await runtime.operations.get(late_id)).state == "running"
                deleted = await api.delete(f"/api/v1/memories/{current_id}")
                assert deleted.status_code == 200, deleted.text
                release.set()
                assert await asyncio.wait_for(worker, 10)
            assert (await runtime.operations.get(late_id)).state == "cancelled"
            stale = await runtime.memory.index.search("tea", runtime.memory.owner, 10)
            assert current_id in {hit.id for hit in stale}, "The forget race must contain the old vector."
            stages["forgotten_with_stale_vector"] = await _read(runtime, api, requests, None)
            stages["forgotten_with_stale_vector"]["rejected_stale_id"] = current_id
            await _drain(runtime)
            assert not await runtime.memory.index.search("tea", runtime.memory.owner, 10)

        async with _running(settings, root, live, requests) as (runtime, api):
            assert (await runtime.operations.get(late_id)).state == "cancelled"
            assert (await runtime.operations.get(identifier)).state == "succeeded"
            assert await runtime.memory.list() == []
            stages["restarted_after_forget"] = await _read(runtime, api, requests, None)
            assert await runtime.memory.list() == []
        assert _fingerprints() == sources, "Source changed during lifecycle measurement."
        report["complete"] = True
    finally:
        _save(root, live, "memory-p2-lifecycle", report)


async def test_restart_correction_and_forget_preserve_receipts_and_current_context(tmp_path: Path) -> None:
    await _lifecycle(tmp_path, settings_at(tmp_path), live=False)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1", reason="Explicit live endpoint opt-in required"
)
async def test_live_restart_correction_and_forget_lifecycle(tmp_path: Path) -> None:
    path = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert path, "Select an existing settings.toml with SHOVEL_LIVE_CONFIG."
    settings = isolated_settings(AppSettings.load(Path(path)), tmp_path)
    assert settings.model.is_configured and settings.memory.embedder.is_configured
    await _lifecycle(tmp_path, settings, live=True)


async def _profile_race(root: Path, settings: AppSettings, *, live: bool, clear: bool, review: bool) -> None:
    settings.memory.review_required = review
    requests: list[list[dict[str, Any]]] = []
    sources = _fingerprints()
    report = _report(settings, live, sources)
    report.update(clear=clear, review_required=review)
    initial = {"revision": 0, "nickname": "Juniper", "gender": "", "occupation": "", "bio": ""}
    try:
        async with _running(settings, root, live, requests) as (runtime, api):
            assert runtime.memory.index is not None
            report["embedding_model_version"] = runtime.memory.index.model_version
            saved = await api.put("/api/v1/user/profile", json=initial)
            assert saved.status_code == 200, saved.text
            previous = await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
            assert len(previous) == 1
            old = previous[0]
            await _drain(runtime)
            answer = await _send(api, "My name is Juniper. I prefer green tea.")
            identifier = answer["memory_operation_id"]
            assert identifier
            async with _paused_worker(runtime) as (release, worker, proposed):
                assert (await runtime.operations.get(identifier)).state == "running"
                assert any(item.predicate == "has_name" and item.object == "Juniper" for item in proposed)
                assert any(item.predicate == "prefers" and item.object == "green tea" for item in proposed)
                updated = await api.put(
                    "/api/v1/user/profile",
                    json={**initial, "revision": 1, "nickname": "" if clear else "Cypress"},
                )
                assert updated.status_code == 200, updated.text
                release.set()
                assert await asyncio.wait_for(worker, 10)
            operation = await runtime.operations.get(identifier)
            assert operation.state == "succeeded", operation.error
            assert operation.result["memories"] == 1
            assert operation.result["candidates"] == int(review)
            report["extraction_receipt"] = operation.result
            records = await runtime.memory.list(include_history=True)
            drink = next(item for item in records if item["object"] == "green tea")
            assert drink["state"] == ("candidate" if review else "active")
            if review:
                approval = await api.post(
                    f"/api/v1/memories/{drink['id']}/review", json={"decision": "approve"}
                )
                assert approval.status_code == 202, approval.text
                assert await runtime.memory_tasks.run_once()
                assert (await runtime.operations.get(approval.json()["id"])).state == "succeeded"
            assert not any(item["object"] == "Juniper" for item in await runtime.memory.list())

        async with _running(settings, root, live, requests) as (runtime, api):
            for _ in range(2):
                await runtime.user_profile.synchronize()
            current = await runtime.memory.repository.profile(runtime.memory.owner, limit=10)
            assert [item.object for item in current] == ([] if clear else ["Cypress"])
            assert runtime.memory.index is not None
            stale = await runtime.memory.index.search("Juniper", runtime.memory.owner, 10)
            assert any(hit.id == old.id and hit.revision == old.revision for hit in stale)
            report["rejected_stale_profile_vector"] = {"id": old.id, "revision": old.revision}
            recalled = await runtime.memory.search("What is my name?")
            assert not any(item["object"] == "Juniper" for item in recalled)
            report["cross_session_after_restart"] = await _read(runtime, api, requests, "green tea")
            profile_payloads = [
                payload["user_profile"] for payload in requests[-1] if "user_profile" in payload
            ]
            assert profile_payloads and profile_payloads[-1]["nickname"] == ("" if clear else "Cypress")
            await _drain(runtime)
            assert runtime.memory.index is not None
            hits = await runtime.memory.index.search("Juniper", runtime.memory.owner, 10)
            assert not any(hit.id == old.id and hit.revision == old.revision for hit in hits)
            assert (await runtime.operations.get(identifier)).state == "succeeded"
        assert _fingerprints() == sources, "Source changed during lifecycle measurement."
        report["complete"] = True
    finally:
        _save(root, live, f"memory-p2-profile-clear-{clear}-review-{review}", report)


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("review", [False, True])
async def test_profile_settings_fence_late_extraction_across_review_and_restart(
    tmp_path: Path, clear: bool, review: bool
) -> None:
    await _profile_race(tmp_path, settings_at(tmp_path), live=False, clear=clear, review=review)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1", reason="Explicit live endpoint opt-in required"
)
@pytest.mark.parametrize("clear", [False, True])
async def test_live_profile_settings_fence_late_extraction_and_review(tmp_path: Path, clear: bool) -> None:
    path = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert path, "Select an existing settings.toml with SHOVEL_LIVE_CONFIG."
    settings = isolated_settings(AppSettings.load(Path(path)), tmp_path)
    assert settings.model.is_configured and settings.memory.embedder.is_configured
    await _profile_race(tmp_path, settings, live=True, clear=clear, review=True)


async def test_memory_diagnostics_separate_extraction_index_filter_and_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[list[dict[str, Any]]] = []
    async with _running(settings_at(tmp_path), tmp_path, False, requests) as (runtime, api):
        source = "I prefer green tea."
        runtime.memory.extractor.extract = AsyncMock(
            return_value=[
                MemoryCandidate(
                    "User prefers green tea.", object="green tea", topic="drink", evidence=source
                ),
                MemoryCandidate(
                    "User prefers green tea.", object="green tea", evidence=source, confidence=0.1
                ),
            ]
        )
        answer = await _send(api, source)
        with development_logging():
            assert await runtime.memory_tasks.run_once()
            assert runtime.memory.index is not None
            runtime.memory.index.embedder.fail = True
            assert await runtime.memory.maintain()
            assert (await runtime.memory.status())["pending_index_operations"] > 0
            assert len(await runtime.memory.search("Which drink do I prefer?")) == 1
            runtime.memory.index.embedder.fail = False
            assert await runtime.memory.search("Which drink did I prefer?") == []
            runtime.memory.application.max_chars = 1
            assert await runtime.memory.search("Which drink do I prefer?") == []
        assert (await runtime.operations.get(answer["memory_operation_id"])).state == "succeeded"
    output = capsys.readouterr().out
    committed = next(line for line in output.splitlines() if "memory.extraction.committed" in line)
    assert all(
        field in committed
        for field in ("proposed_candidates=2", "admitted_candidates=1", "committed_records=1")
    )
    index = next(line for line in output.splitlines() if "memory.index.completed" in line)
    assert "failed=1" in index and "pending_batch=1" in index and "upserted=0" in index
    recalled = [line for line in output.splitlines() if "memory.recall.completed" in line]
    assert len(recalled) == 3 and "vector_fallback=True" in recalled[0]
    assert "support_unknown=1" in recalled[1]
    assert "budget_skipped=1" in recalled[2] and "returned_items=0" in recalled[2]
    assert "green tea" not in output and str(tmp_path) not in output and "test-private-key" not in output


async def test_final_agent_memory_budget_reports_drops_without_claiming_support(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = settings_at(tmp_path)
    settings.performance.context_profile = "compact"
    requests: list[list[dict[str, Any]]] = []
    async with _running(settings, tmp_path, False, requests) as (runtime, api):
        await runtime.memory.repository.consolidate(
            runtime.memory.owner,
            [
                MemoryCandidate(
                    "User uses SQLite for project Meridian. " + "Reference fragment. " * 60,
                    predicate="uses",
                    object="SQLite",
                    topic="project.meridian.database",
                )
            ],
            Provenance("manual"),
        )
        with development_logging():
            await _send(api, "Which database do I use for project Meridian?")
        statuses = [p["memory_status"] for p in requests[-1] if "memory_status" in p]
        assert statuses == ["no_supported_fact"]
        assert not any(p.get("memory") for p in requests[-1])
    output = capsys.readouterr().out
    recalled = next(line for line in output.splitlines() if "memory.recall.completed" in line)
    assert "returned_items=1" in recalled
    context = next(line for line in output.splitlines() if "agent.context.ready" in line)
    assert (
        "recalled_memories=1" in context and "memory_budget_skipped=1" in context and "memories=0" in context
    )
    assert "Reference fragment" not in output and "Meridian" not in output and "SQLite" not in output
