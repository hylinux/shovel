from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from scripts.memory_baseline import baseline_runtime, isolated_settings

from shovel.config.settings import AppSettings, load_settings
from shovel.db.model.runtime import Conversation
from shovel.memory.domain import Provenance
from shovel.pipeline.embedder import build_embedder
from shovel.runtime.hosting import build_host
from shovel.runtime.schemas import MessageInput, SearchInput
from shovel.services.config_server import shovel_context
from shovel.vector.store import init_vector_store
from test_knowledge_baseline import index_corpus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1",
        reason="Explicit live endpoint opt-in required",
    ),
]

_REPO = Path(__file__).resolve().parents[2]
_CONFIGURATION = "/api/v1/runtime/configuration"


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def selected_settings() -> Iterator[AppSettings]:
    path = Path(os.environ["SHOVEL_LIVE_CONFIG"])
    original = hashlib.sha256(path.read_bytes()).digest()
    settings = AppSettings.load(path)
    assert settings.model.is_configured and settings.embedding.is_configured
    assert settings.memory.embedder.is_configured
    yield settings
    assert hashlib.sha256(path.read_bytes()).digest() == original, "User configuration was modified."


class Evidence:
    def __init__(self, path: Path, configured: AppSettings) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "scope": "issue23-selected-configuration",
            "models": {
                "main": {
                    "provider": configured.model.model_provider_type.value,
                    "model": configured.model.model_name,
                    "authentication": "configured-key" if configured.model.security_key else "empty-key",
                },
                "memory": {
                    "provider": configured.memory.llm.model_provider_type.value,
                    "model": configured.memory.llm.model_name,
                    "inherits_main": not configured.memory.llm.is_configured,
                },
                "knowledge_embedding": {
                    "provider": configured.embedding.provider,
                    "model": configured.embedding.model,
                    "dimension": configured.zvec.dim,
                },
                "memory_embedding": {
                    "provider": configured.memory.embedder.provider,
                    "model": configured.memory.embedder.model,
                    "dimension": configured.memory.dim,
                },
            },
            "checks": [],
            "complete": False,
        }

    def record(self, name: str, **details: Any) -> None:
        self.data["checks"].append({"name": name, "passed": True, **details})
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def evidence(
    request: pytest.FixtureRequest, tmp_path: Path, selected_settings: AppSettings
) -> Iterator[Evidence]:
    directory = Path(os.environ.get("SHOVEL_RUNTIME_CONFIG_LIVE_REPORT_DIR", str(tmp_path)))
    report = Evidence(directory / f"{request.node.name}.json", selected_settings)
    yield report
    report.flush()


async def test_live_configured_model_and_embedding_chains(
    selected_settings: AppSettings, tmp_path: Path, evidence: Evidence
) -> None:
    settings = isolated_settings(selected_settings, tmp_path)
    for label, configured, dimension in (
        ("knowledge_embedding", settings.embedding, settings.zvec.dim),
        ("memory_embedding", settings.memory.embedder, settings.memory.dim),
    ):
        embedder = build_embedder(configured, expected_dimension=dimension)
        try:
            vectors = await embedder.embed(["Synthetic runtime configuration acceptance probe."])
            assert len(vectors) == 1 and len(vectors[0]) == dimension
            assert all(math.isfinite(value) for value in vectors[0])
            evidence.record(label, dimension=len(vectors[0]))
        finally:
            await embedder.aclose()
    init_vector_store(settings.zvec)
    async with baseline_runtime(settings) as runtime:
        async with runtime.sessions() as database:
            session = Conversation(title="Synthetic configuration acceptance")
            database.add(session)
            await database.commit()
        answer = await runtime.chat.send(
            session.id,
            MessageInput(
                content="For this synthetic acceptance test only, reply with exactly CONFIG23_READY.",
                memory_enabled=False,
            ),
        )
        assert answer.state == "completed" and "CONFIG23_READY" in answer.content
        evidence.record("main_agent_real_response")
        source = "I prefer jasmine tea every morning."
        candidates = await runtime.memory.extractor.extract(source)
        assert candidates and any("tea" in item.object.lower() for item in candidates)
        assert all(item.evidence in source for item in candidates)
        summary = await runtime.memory.extractor.summarize(
            "",
            [{"role": "user", "content": "The synthetic project Cedar will ship on Friday."}],
            500,
        )
        assert summary and len(summary) <= 500
        evidence.record("memory_llm_extraction_and_summary", candidates=len(candidates))
        records = await runtime.memory.repository.consolidate(
            runtime.memory.owner, candidates, Provenance("manual")
        )
        assert records
        await runtime.memory.rebuild()
        assert (await runtime.memory.status())["index_state"] == "ready"
        assert runtime.memory.index is not None
        hits = await runtime.memory.index.search("What tea do I prefer?", runtime.memory.owner, 5)
        assert any(hit.id in {record.id for record in records} for hit in hits)
        evidence.record("memory_real_vectors_persisted_and_recalled")
        await index_corpus(runtime, tmp_path, embed=True)
        results = await runtime.retrieval.search(SearchInput(query="离线备份", mode="hybrid", limit=3))
        assert results["items"]
        grounded = await runtime.chat.send(
            session.id,
            MessageInput(
                content="请根据本地资料简述离线备份要求,并引用来源。",
                search_mode="hybrid",
                memory_enabled=False,
            ),
        )
        assert grounded.state == "completed" and grounded.citations
        evidence.record("knowledge_real_scan_hybrid_retrieval_and_grounded_answer")
    evidence.data["complete"] = True


@asynccontextmanager
async def _host(
    path: Path, *, development: bool = False, environment: dict[str, str] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    settings = AppSettings.load(path)
    port = settings.dev_http_port if development else settings.http_port
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("SHOVEL_")}
    env.update(environment or {})
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join((str(_REPO / "backend" / "src"), str(_REPO)))
    log_path = path.parent / f"host-{port}.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__)),
            "--host",
            str(path),
            "dev" if development else "prd",
            str(_REPO / "frontend" / "out"),
            cwd=_REPO,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=360, trust_env=False
            ) as client:
                deadline = monotonic() + 90
                while True:
                    assert process.returncode is None, "Isolated Host exited before readiness."
                    try:
                        ready = await client.get("/api/v1/status", timeout=2)
                    except httpx.TransportError:
                        ready = None
                    if ready is not None and ready.status_code == 200:
                        break
                    assert monotonic() < deadline, "Isolated Host readiness timed out."
                    await asyncio.sleep(0.25)
                yield client
        finally:
            assert process.stdin is not None
            if process.returncode is None:
                process.stdin.write(b"stop\n")
                await process.stdin.drain()
                try:
                    await asyncio.wait_for(process.wait(), 60)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    pytest.fail("Isolated Host did not shut down gracefully.")
            assert process.returncode == 0, "Isolated Host shutdown failed."


async def _view(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(_CONFIGURATION)
    assert response.status_code == 200, "Configuration read failed."
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert isinstance(result, dict)
    return result


async def _save(
    client: httpx.AsyncClient, view: dict[str, Any], changes: dict[str, Any], status: int = 200
) -> dict[str, Any]:
    response = await client.put(_CONFIGURATION, json={"revision": view["revision"], "changes": changes})
    assert response.status_code == status, (
        f"Configuration save expected {status}, received {response.status_code}; fields: {', '.join(changes)}"
    )
    result = response.json()
    assert isinstance(result, dict)
    return result


async def _chat_probe(client: httpx.AsyncClient, identifier: str | None = None) -> str:
    if identifier is None:
        response = await client.post("/api/v1/conversations", json={"title": "Synthetic config test"})
        assert response.status_code == 201
        identifier = response.json()["id"]
    response = await client.post(
        f"/api/v1/conversations/{identifier}/messages",
        json={
            "content": "Reply with exactly CONFIG23_READY for this synthetic acceptance test.",
            "memory_enabled": False,
        },
    )
    assert response.status_code == 200, "Configured Main Agent live call failed."
    assert response.json()["state"] == "completed" and "CONFIG23_READY" in response.json()["content"]
    return identifier


async def test_live_runtime_configuration_host_restart_and_field_matrix(
    selected_settings: AppSettings, tmp_path: Path, evidence: Evidence
) -> None:
    settings = isolated_settings(selected_settings, tmp_path)
    settings.http_host = settings.dev_http_host = "127.0.0.1"
    settings.http_port, settings.dev_http_port, settings.dev_frontend_port = _port(), _port(), _port()
    settings.performance.warm_index = settings.performance.warm_local_models = False
    path = tmp_path / "profile" / "config" / "settings.toml"
    settings.save(path)
    init_vector_store(settings.zvec)
    original = settings.model_dump(mode="python")
    try:
        async with _host(path) as client:
            view = await _view(client)
            assert view["applied"] and view["editable"]
            fixed = {item["key"]: item["value"] for item in view["items"] if item["readonly"]}
            secrets = [item["key"] for item in view["items"] if item["secret"]]
            assert all(item["value"] is None for item in view["items"] if item["secret"])
            assert (await client.get("/")).status_code == 200
            evidence.record("production_static_export_and_monitoring")
            conversation = await _chat_probe(client)
            evidence.record("initial_host_main_agent")
            for key in fixed:
                await _save(client, view, {key: "not-an-editable-path"}, status=422)
            forbidden = await client.put(
                _CONFIGURATION,
                json={"revision": view["revision"], "changes": {}, "path": str(tmp_path / "other")},
            )
            assert forbidden.status_code == 422
            evidence.record("all_fixed_paths_and_config_path_rejected", fields=list(fixed))
            for invalid_changes in (
                {"http_host": "0.0.0.0"},
                {"dev_http_host": "::1"},
                {"http_port": 0},
                {"performance.max_concurrent_scans": 65},
                {"embedding.dimensions": settings.zvec.dim + 1},
                {"model.max_output_tokens": "not-an-integer"},
                {"embedding.provider": "deepseek", "embedding.model": "chat-only"},
            ):
                await _save(client, view, invalid_changes, status=422)
            evidence.record("invalid_types_ranges_listeners_dimensions_and_provider_rejected")
            for key in secrets:
                replaced = await _save(client, view, {key: "synthetic-acceptance-value"})
                assert "synthetic-acceptance-value" not in json.dumps(replaced)
                preserved = await _save(client, replaced, {})
                assert next(item for item in preserved["items"] if item["key"] == key)["configured"]
                cleared = await _save(client, preserved, {key: ""})
                assert not next(item for item in cleared["items"] if item["key"] == key)["configured"]
                # Restore only from the selected file; never send saved keys back in API responses.
                section: Any = original
                for part in key.split("."):
                    section = section[part]
                restored = section.get_secret_value() if hasattr(section, "get_secret_value") else section
                view = await _save(client, cleared, {key: restored})
            evidence.record("all_four_secret_keep_replace_clear_and_restore", fields=secrets)
            await _save(client, view, {"zvec.dim": settings.zvec.dim + 1}, status=422)
            evidence.record("existing_knowledge_index_dimension_change_rejected")
            # Round-trip every editor field with the selected model identity intact.
            changes = {
                item["key"]: item["value"]
                for item in view["items"]
                if not item["readonly"] and not item["secret"]
            }
            changes.update(
                {
                    "service_name": "shovel-config23-live",
                    "http_port": _port(),
                    "dev_http_port": _port(),
                    "dev_frontend_port": _port(),
                    "frontend_path": str(_REPO / "frontend" / "out"),
                    "agent.max_tool_iterations": max(1, settings.agent.max_tool_iterations - 1),
                    "performance.max_concurrent_scans": 2,
                    "model.max_output_tokens": settings.model.max_output_tokens + 1,
                    "memory.llm.max_output_tokens": settings.memory.llm.max_output_tokens + 1,
                }
            )
            saved = await _save(client, view, changes)
            assert not saved["applied"]
            assert {item["key"]: item["value"] for item in saved["items"] if item["readonly"]} == fixed
            by_key = {item["key"]: item for item in saved["items"]}
            assert by_key["http_port"]["active_value"] == settings.http_port
            for key, value in changes.items():
                assert by_key[key]["value"] == value, f"Field round-trip failed: {key}"
            evidence.record("every_editable_field_roundtrip", fields=sorted(changes) + secrets)
            await _save(client, view, {"service_name": "stale"}, status=409)
            pending = await client.post(_CONFIGURATION + "/check", json={"revision": saved["revision"]})
            assert pending.status_code == 200 and not pending.json()["applied"]
            assert (await client.post("/api/v1/agent/restart")).status_code == 200
            assert not (await _view(client))["applied"]
            assert (await client.get("/api/v1/status")).status_code == 200
            evidence.record("stale_revision_rejected_agent_restart_not_misreported_as_application")
        async with _host(path) as client:
            restarted = await _view(client)
            assert restarted["applied"] and restarted["revision"] == saved["revision"]
            assert (
                next(item for item in restarted["items"] if item["key"] == "service_name")["active_value"]
                == "shovel-config23-live"
            )
            await _chat_probe(client, conversation)
            checked = await client.post(_CONFIGURATION + "/check", json={"revision": saved["revision"]})
            assert checked.status_code == 200 and checked.json()["applied"]
            evidence.record("new_host_process_new_port_reconnect_live_chat_and_preserved_session")
        async with _host(
            path,
            development=True,
            environment={"SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS": "3"},
        ) as client:
            env_view = await _view(client)
            field = next(
                item for item in env_view["items"] if item["key"] == "performance.max_concurrent_scans"
            )
            assert field["value"] == 2 and field["active_value"] == field["effective_value"] == 3
            assert field["environment"] == ["SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS"]
            assert env_view["applied"]
            await _chat_probe(client, conversation)
            evidence.record("development_host_new_port_environment_override_and_live_chat")
            broken = await _save(client, env_view, {"model.base_endpoint": f"http://127.0.0.1:{_port()}"})
            assert not broken["applied"]
        async with _host(path) as client:
            assert (await _view(client))["applied"]
            failure = await client.post(
                f"/api/v1/conversations/{conversation}/messages",
                json={"content": "Synthetic failure test.", "memory_enabled": False},
            )
            assert failure.status_code in {502, 503}
            assert "Traceback" not in failure.text and "Bearer " not in failure.text
            evidence.record("provider_failure_is_explicit_not_a_successful_answer")
            repaired = await _save(
                client,
                await _view(client),
                {
                    "model.base_endpoint": selected_settings.model.base_endpoint,
                },
            )
            assert not repaired["applied"]
        async with _host(path) as client:
            assert (await _view(client))["applied"]
            await _chat_probe(client, conversation)
            evidence.record("provider_failure_recovery_after_full_restart")
        evidence.data["complete"] = True
    finally:
        path.unlink(missing_ok=True)


async def _serve(path: Path, development: bool, frontend: Path) -> None:
    settings = load_settings(path)
    with patch.object(shovel_context, "get_default_config_file", return_value=path):
        host = build_host(settings, development=development, frontend=None if development else frontend)
        try:
            await host.start_async()
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            await host.stop_async()


if __name__ == "__main__":
    assert sys.argv[1] == "--host"
    asyncio.run(_serve(Path(sys.argv[2]), sys.argv[3] == "dev", Path(sys.argv[4])))
