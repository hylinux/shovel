from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from shovel.config.settings import AppSettings, load_settings
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.runtime_config import RuntimeConfigInput, RuntimeConfigurationService
from shovel.runtime.service import Runtime
from shovel.services.config_server import shovel_context
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


@pytest.fixture
def configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeConfigurationService:
    path = tmp_path / "profile" / "config" / "settings.toml"
    monkeypatch.setattr(shovel_context, "get_default_config_file", lambda: path)
    active = AppSettings.from_file_data(
        {
            "agent": {"workspace_path": str(tmp_path / "workspace")},
            "database": {"url": f"sqlite+aiosqlite:///{tmp_path.as_posix()}/store.db"},
            "zvec": {"knowledge_path": str(tmp_path / "knowledge")},
            "memory": {
                "index_path": str(tmp_path / "memory"),
                "embedder": {"api_key": "memory-vector-private"},
                "llm": {
                    "model_provider_type": "deepseek",
                    "model_name": "memory-chat",
                    "security_key": "memory-chat-private",
                },
                "review_required": True,
            },
            "model": {
                "model_provider_type": "deepseek",
                "model_name": "deepseek-chat",
                "security_key": "chat-private",
                "max_input_images": 7,
            },
            "embedding": {"api_key": "vector-private"},
            "document": {"root_dir": "keep-document-root"},
        }
    )
    active.save(path)
    return RuntimeConfigurationService(active)


def save(service: RuntimeConfigurationService, changes: dict[str, Any]) -> dict[str, Any]:
    return service.save(RuntimeConfigInput(revision=service.read()["revision"], changes=changes))


def test_save_preserves_hidden_fields_fixed_paths_and_secrets_and_verifies_restart(
    configuration: RuntimeConfigurationService,
) -> None:
    before = tomllib.loads(configuration.file.path.read_text())
    view = configuration.read()
    assert view["applied"] and view["editable"]
    assert "private" not in json.dumps(view)
    saved = save(configuration, {"performance.max_concurrent_scans": 8, "log_level": "DEBUG"})
    assert saved["pending"] == ["log_level", "performance.max_concurrent_scans"]
    assert configuration.active.log_level == "INFO"
    assert not configuration.check_application(saved["revision"])["applied"]
    raw = tomllib.loads(configuration.file.path.read_text())
    for key in ("model", "memory", "agent", "zvec", "database", "document", "platform"):
        assert raw[key] == before[key]
    with pytest.raises(RuntimeFailure, match="Reload"):
        configuration.check_application(view["revision"])
    restarted = RuntimeConfigurationService(AppSettings.load(configuration.file.path))
    assert restarted.check_application(saved["revision"])["applied"]


@pytest.mark.parametrize("section", ["model", "memory.llm"])
@pytest.mark.parametrize("budget", [32769, 128000])
def test_output_budget_above_previous_ceiling_round_trips(
    configuration: RuntimeConfigurationService, section: str, budget: int
) -> None:
    key = f"{section}.max_output_tokens"
    saved = save(configuration, {key: budget})
    item = next(item for item in saved["items"] if item["key"] == key)
    assert item["value"] == budget
    assert item["active_value"] == 2048
    assert item["minimum"] == 1 and item["maximum"] is None
    assert item["pending"]
    restarted = RuntimeConfigurationService(AppSettings.load(configuration.file.path))
    reloaded = next(item for item in restarted.read()["items"] if item["key"] == key)
    assert reloaded["active_value"] == budget
    assert restarted.read()["applied"]


@pytest.mark.parametrize("section", ["model", "memory.llm"])
def test_output_budget_environment_override_above_previous_ceiling(
    configuration: RuntimeConfigurationService, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    key = f"{section}.max_output_tokens"
    env_key = "SHOVEL_" + key.replace(".", "__").upper()
    monkeypatch.setenv(env_key, "128000")
    saved = save(configuration, {key: 32769})
    item = next(item for item in saved["items"] if item["key"] == key)
    assert (item["value"], item["effective_value"]) == (32769, 128000)
    assert item["environment"] == [env_key]
    restarted = RuntimeConfigurationService(load_settings(configuration.file.path))
    assert restarted.read()["applied"]
    assert next(item for item in restarted.read()["items"] if item["key"] == key)["active_value"] == 128000


@pytest.mark.parametrize(
    "key",
    [
        "agent.workspace_path",
        "database.url",
        "zvec.knowledge_path",
        "memory.index_path",
        "path",
        "config_path",
        "temp_path",
        "document.root_dir",
        "platform",
        "model",
        "zvec.chunk_collection",
    ],
)
def test_disallowed_paths_and_sections_cannot_be_written(
    configuration: RuntimeConfigurationService, key: str
) -> None:
    before = configuration.file.path.read_bytes()
    with pytest.raises(RuntimeFailure) as error:
        save(configuration, {key: "somewhere-else"})
    assert error.value.status == 422
    assert configuration.file.path.read_bytes() == before


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"http_host": "0.0.0.0"}, "http_host"),
        ({"dev_http_host": "remote.example"}, "dev_http_host"),
        ({"dev_http_host": "::1"}, "dev_http_host"),
        ({"agent_host": "::"}, "agent_host"),
        ({"http_port": 0}, "http_port"),
        ({"agent_port": 65536}, "agent_port"),
        ({"dev_frontend_port": 9999}, "dev_frontend_port"),
        ({"log_level": "secret-invalid"}, "log_level"),
        ({"service_name": " "}, "service_name"),
        ({"performance.max_concurrent_scans": 65}, "performance.max_concurrent_scans"),
        ({"model.max_output_tokens": 0}, "model.max_output_tokens"),
        ({"memory.llm.max_output_tokens": -1}, "memory.llm.max_output_tokens"),
        ({"agent.max_tool_iterations": "12"}, "agent.max_tool_iterations"),
        ({"memory.min_confidence": 2}, "memory.min_confidence"),
        ({"embedding.dimensions": 3}, "embedding.dimensions"),
        ({"zvec.dim": -1}, "zvec.dim"),
        ({"model.model_provider_type": "kimi"}, "model.model_provider_type"),
        ({"embedding.provider": "deepseek", "embedding.model": "chat-only"}, "embedding.provider"),
        ({"model.model_name": ""}, "model.model_name"),
        ({"model.base_endpoint": "https://user:private@example.test"}, "model.base_endpoint"),
        ({"model.base_endpoint": "http://["}, "model.base_endpoint"),
        ({"model.base_endpoint": "https://example.test:not-a-port"}, "model.base_endpoint"),
        ({"embedding.timeout_seconds": float("inf")}, "embedding.timeout_seconds"),
        ({"memory.collection": " "}, "memory.collection"),
        ({"model.security_key": "**********"}, "model.security_key"),
    ],
)
def test_invalid_fields_never_write_or_echo_inputs(
    configuration: RuntimeConfigurationService, changes: dict[str, Any], field: str
) -> None:
    before = configuration.file.path.read_bytes()
    with pytest.raises(RuntimeFailure) as error:
        save(configuration, changes)
    assert error.value.status == 422
    assert field in str(error.value)
    assert "private" not in str(error.value) and "secret-invalid" not in str(error.value)
    assert configuration.file.path.read_bytes() == before


@pytest.mark.parametrize("section", ["model", "memory.llm", "embedding", "memory.embedder"])
def test_secret_keep_replace_and_clear(configuration: RuntimeConfigurationService, section: str) -> None:
    key = f"{section}.{'security_key' if section in {'model', 'memory.llm'} else 'api_key'}"
    save(configuration, {"log_level": "DEBUG"})
    assert next(item for item in configuration.read()["items"] if item["key"] == key)["configured"]
    replaced = save(configuration, {key: "replacement-private"})
    assert "replacement-private" in configuration.file.path.read_text()
    assert "replacement-private" not in json.dumps(replaced)
    cleared = save(configuration, {key: ""})
    item = next(item for item in cleared["items"] if item["key"] == key)
    assert not item["configured"] and item["value"] is None


@pytest.mark.parametrize("section", ["model", "memory.llm"])
@pytest.mark.parametrize("provider", ["azure_openai", "deepseek", "qianwen", "ollama"])
def test_chat_provider_configuration(
    configuration: RuntimeConfigurationService, section: str, provider: str
) -> None:
    changes = {f"{section}.model_provider_type": provider, f"{section}.model_name": "deployment"}
    if provider == "azure_openai":
        changes.update(
            {
                f"{section}.base_endpoint": "https://resource.openai.azure.com",
                f"{section}.api_version": "2025-04-01-preview",
                f"{section}.security_key": "",
            }
        )
    result = save(configuration, changes)
    assert result["pending"] and "private" not in json.dumps(result)


@pytest.mark.parametrize("section", ["embedding", "memory.embedder"])
@pytest.mark.parametrize("provider", ["azure_openai", "qianwen", "ollama"])
def test_embedding_provider_configuration(
    configuration: RuntimeConfigurationService, section: str, provider: str
) -> None:
    changes: dict[str, Any] = {
        f"{section}.provider": provider,
        f"{section}.model": "embedding-model",
        f"{section}.base_url": "https://embedding.example.test",
    }
    if provider == "azure_openai":
        changes[f"{section}.api_version"] = "2025-04-01-preview"
        changes[f"{section}.dimensions"] = 1024
        changes[f"{section}.api_key"] = ""
    result = save(configuration, changes)
    assert result["pending"]


def test_provider_specific_errors_and_optional_dimensions(configuration: RuntimeConfigurationService) -> None:
    for changes, field in (
        ({"model.model_provider_type": "azure_openai"}, "model.api_version"),
        ({"embedding.provider": "qianwen", "embedding.model": "embedding"}, "embedding.base_url"),
        (
            {"embedding.provider": "ollama", "embedding.model": "embed", "embedding.dimensions": 1024},
            "embedding",
        ),
    ):
        with pytest.raises(RuntimeFailure, match=field):
            save(configuration, changes)
    save(configuration, {"embedding.dimensions": 1024})
    save(configuration, {"embedding.dimensions": None})
    assert "dimensions" not in tomllib.loads(configuration.file.path.read_text())["embedding"]


def test_environment_source_and_actual_application_are_distinct(
    configuration: RuntimeConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOVEL_MODEL", '{"security_key":"environment-private"}')
    monkeypatch.setenv("SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS", "12")
    saved = save(configuration, {"performance.max_concurrent_scans": 8})
    item = next(item for item in saved["items"] if item["key"] == "performance.max_concurrent_scans")
    assert (item["value"], item["active_value"], item["effective_value"]) == (8, 5, 12)
    assert item["environment"] == ["SHOVEL_PERFORMANCE__MAX_CONCURRENT_SCANS"]
    assert "private" not in json.dumps(saved)
    restarted = RuntimeConfigurationService(load_settings(configuration.file.path))
    assert restarted.read()["applied"]
    assert (
        "SHOVEL_MODEL"
        in next(item for item in restarted.read()["items"] if item["key"] == "model.security_key")[
            "environment"
        ]
    )


@pytest.mark.parametrize(
    "changes,index",
    [
        ({"embedding.model": "new-embed", "embedding.provider": "ollama"}, "knowledge"),
        ({"zvec.dim": 512}, "knowledge"),
        ({"memory.collection": "different"}, "memory"),
        ({"memory.dim": 512}, "memory"),
        ({"memory.embedder.model": "new-embed", "memory.embedder.provider": "ollama"}, "memory"),
    ],
)
def test_existing_vectors_require_controlled_rebuild(
    configuration: RuntimeConfigurationService, changes: dict[str, Any], index: str
) -> None:
    path = configuration.file.path.parents[2] / index
    path.mkdir()
    marker = path / "existing-vectors"
    marker.write_bytes(b"retain-vector-data")
    before = configuration.file.path.read_bytes()
    with pytest.raises(RuntimeFailure, match="controlled rebuild"):
        save(configuration, changes)
    assert configuration.file.path.read_bytes() == before
    assert marker.read_bytes() == b"retain-vector-data"


def test_external_edits_and_failed_atomic_write_preserve_original(
    configuration: RuntimeConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = configuration.read()
    configuration.file.path.write_text(configuration.file.path.read_text() + "\n# external edit\n")
    with pytest.raises(RuntimeFailure) as error:
        configuration.save(RuntimeConfigInput(revision=initial["revision"], changes={"log_level": "DEBUG"}))
    assert error.value.status == 409
    before = configuration.file.path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError("private-error-detail")

    monkeypatch.setattr("shovel.runtime.workspace_config.os.replace", fail_replace)
    with pytest.raises(RuntimeFailure) as error:
        save(configuration, {"log_level": "DEBUG"})
    assert error.value.status == 503 and "private-error-detail" not in str(error.value)
    assert configuration.file.path.read_bytes() == before
    assert not list(configuration.file.path.parent.glob("*.tmp"))


async def test_runtime_api_redaction_fixed_profile_and_failed_application_check(
    runtime: Runtime, api: httpx.AsyncClient, tmp_path: Path
) -> None:
    from shovel.runtime.workspace_config import WorkspaceTextFile

    path = tmp_path / "settings.toml"
    runtime.settings.save(path)
    runtime.configuration.file = WorkspaceTextFile(path)
    response = await api.get("/api/v1/runtime/configuration")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "test-private-key" not in response.text
    view = response.json()
    invalid = await api.put(
        "/api/v1/runtime/configuration",
        json={
            "revision": view["revision"],
            "changes": {"agent.workspace_path": "different"},
        },
    )
    assert invalid.status_code == 422
    wrong_path = await api.put(
        "/api/v1/runtime/configuration",
        json={
            "revision": view["revision"],
            "changes": {},
            "path": str(tmp_path / "alternate.toml"),
        },
    )
    assert wrong_path.status_code == 422
    saved = await api.put(
        "/api/v1/runtime/configuration",
        json={
            "revision": view["revision"],
            "changes": {"log_level": "DEBUG"},
        },
    )
    assert saved.status_code == 200 and not saved.json()["applied"]
    pending = await api.post(
        "/api/v1/runtime/configuration/check", json={"revision": saved.json()["revision"]}
    )
    assert pending.status_code == 200 and pending.json()["pending"] == ["log_level"]
    stale = await api.post("/api/v1/runtime/configuration/check", json={"revision": view["revision"]})
    assert stale.status_code == 409
    assert runtime.settings.log_level == "INFO"
    assert (await api.get("/api/v1/status")).status_code == 200


def test_hidden_external_changes_are_not_reported_as_applied(
    configuration: RuntimeConfigurationService,
) -> None:
    text = configuration.file.path.read_text().replace("max_input_images = 7", "max_input_images = 8")
    configuration.file.path.write_text(text)
    assert not configuration.read()["applied"]


def test_legacy_removed_fields_are_preserved_without_rejecting_an_existing_profile(
    configuration: RuntimeConfigurationService,
) -> None:
    configuration.file.path.write_text("redis = { host = 'legacy' }\n" + configuration.file.path.read_text())
    save(configuration, {"log_level": "DEBUG"})
    assert tomllib.loads(configuration.file.path.read_text())["redis"] == {"host": "legacy"}


def test_environment_cannot_bypass_loopback_validation(
    configuration: RuntimeConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOVEL_HTTP_HOST", "0.0.0.0")
    before = configuration.file.path.read_bytes()
    with pytest.raises(RuntimeFailure, match="http_host"):
        save(configuration, {"http_host": "localhost"})
    assert configuration.file.path.read_bytes() == before


def test_different_profile_stores_disable_all_edits(
    configuration: RuntimeConfigurationService,
) -> None:
    configuration.active.agent.workspace_path = configuration.active.agent.workspace_path / "different"
    assert not configuration.read()["editable"]
    with pytest.raises(RuntimeFailure) as error:
        save(configuration, {"log_level": "DEBUG"})
    assert error.value.status == 409
