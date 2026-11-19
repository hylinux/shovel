from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from shovel.config.embedding_config import EmbeddingSettings
from shovel.config.errors import ConfigError
from shovel.config.metadata import PRODUCT
from shovel.config.settings import AppSettings, load_settings
from shovel.pipeline.embedder import HttpEmbedder
from shovel.runtime.schemas import ProfileInput


def test_environment_overrides_file_but_file_only_load_is_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('http_port = 8081\n[embedding]\nmodel = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("SHOVEL_HTTP_PORT", "8082")
    monkeypatch.setenv("SHOVEL_EMBEDDING__MODEL", "from-environment")
    combined = load_settings(config)
    assert combined.http_port == 8082
    assert combined.embedding.model == "from-environment"
    file_only = AppSettings.load(config)
    assert file_only.http_port == 8081
    assert file_only.embedding.model == "from-file"
    assert load_settings(config).http_port == 8082


def test_removed_agent_listener_settings_load_and_are_omitted_when_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "settings.toml"
    original = (
        'agent_host = "legacy.invalid"\nagent_port = 0\n'
        'http_host = "localhost"\nhttp_port = 8081\n'
        'dev_http_host = "127.0.0.1"\ndev_http_port = 9998\ndev_frontend_port = 3001\n'
    )
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("SHOVEL_AGENT_HOST", "ignored.invalid")
    monkeypatch.setenv("SHOVEL_AGENT_PORT", "not-a-port")
    for settings in (AppSettings.load(config), load_settings(config)):
        assert not {"agent_host", "agent_port"} & settings.model_dump().keys()
        assert (settings.http_host, settings.http_port) == ("localhost", 8081)
        assert (settings.dev_http_host, settings.dev_http_port, settings.dev_frontend_port) == (
            "127.0.0.1",
            9998,
            3001,
        )
    assert config.read_text(encoding="utf-8") == original
    settings.save(config, overwrite=True, backup=False)
    assert "agent_host" not in config.read_text(encoding="utf-8")
    assert "agent_port" not in config.read_text(encoding="utf-8")
    assert AppSettings.load(config) == settings


def test_removed_listener_migration_does_not_hide_unknown_settings(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('agent_host = "legacy"\nagent_prt = 19566\n', encoding="utf-8")
    for loader in (AppSettings.load, load_settings):
        with pytest.raises(ConfigError, match="agent_prt"):
            loader(config)


@pytest.mark.parametrize("timeout,literal", [(300.0, "300"), (300.5, "300.5")])
def test_timeout_toml_format_preserves_integer_and_fractional_seconds(
    tmp_path: Path, timeout: float, literal: str
) -> None:
    config = tmp_path / "settings.toml"
    settings = AppSettings.from_file_data(
        {
            "database": {"url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"},
            "model": {"timeout_seconds": timeout},
            "embedding": {"timeout_seconds": timeout},
            "memory": {
                "llm": {"timeout_seconds": timeout},
                "embedder": {"timeout_seconds": timeout},
            },
        }
    )
    settings.save(config)
    text = config.read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if line.startswith("timeout_seconds =")] == [
        f"timeout_seconds = {literal}"
    ] * 4
    restored = AppSettings.load(config)
    assert restored == settings
    assert settings.model.timeout_seconds == timeout
    assert isinstance(settings.model.timeout_seconds, float)


def test_embedding_identity_tracks_deployment_not_credentials() -> None:
    config = EmbeddingSettings(provider="openai", model="same-name", base_url="https://first.invalid/v1")
    original = HttpEmbedder(config, expected_dimension=4, client=object()).model_version
    for update in (
        {"base_url": "https://second.invalid/v1"},
        {"revision": "new-weights"},
        {"max_input_chars": 9000},
        {"api_version": "new-api"},
    ):
        changed = HttpEmbedder(config.model_copy(update=update), expected_dimension=4, client=object())
        assert changed.model_version != original
    assert HttpEmbedder(config, expected_dimension=8, client=object()).model_version != original
    assert (
        HttpEmbedder(config.model_copy(update={"dimensions": 4}), expected_dimension=4).model_version
        != original
    )
    assert (
        HttpEmbedder(config.model_copy(update={"dimensions": None}), expected_dimension=4).model_version
        == original
    )
    rotated = config.model_copy(update={"api_key": SecretStr("rotated"), "timeout_seconds": 30})
    assert HttpEmbedder(rotated, expected_dimension=4, client=object()).model_version == original


def test_embedding_dimensions_load_independently_from_file_and_environment(tmp_path, monkeypatch):
    config = tmp_path / "settings.toml"
    config.write_text(
        "[embedding]\ndimensions = 1024\n[memory.embedder]\ndimensions = 512\n", encoding="utf-8"
    )
    saved = AppSettings.load(config)
    assert (saved.embedding.dimensions, saved.memory.embedder.dimensions) == (1024, 512)
    monkeypatch.setenv("SHOVEL_EMBEDDING__DIMENSIONS", "256")
    monkeypatch.setenv("SHOVEL_MEMORY__EMBEDDER__DIMENSIONS", "128")
    combined = load_settings(config)
    assert (combined.embedding.dimensions, combined.memory.embedder.dimensions) == (256, 128)
    assert (
        AppSettings.load(config).embedding.dimensions,
        AppSettings.load(config).memory.embedder.dimensions,
    ) == (
        1024,
        512,
    )


@pytest.mark.parametrize(
    "pipeline",
    [
        {"chunker": {"unknown": 2}},
        {"parser": {"max_chars": 0}},
        {"chunker": {"overlap_chars": 900}},
        {"chunker": {"chunk_chars": -1}},
        {"unknown": {}},
    ],
)
def test_profile_rejects_unsupported_or_invalid_pipeline(pipeline: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProfileInput(pipeline=pipeline)


def test_release_versions_agree() -> None:
    import json
    import tomllib

    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    frontend = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert project["project"]["version"] == frontend["version"] == PRODUCT.default_version
