from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from shovel.config.embedding_config import EmbeddingSettings
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
    rotated = config.model_copy(update={"api_key": SecretStr("rotated"), "timeout_seconds": 30})
    assert HttpEmbedder(rotated, expected_dimension=4, client=object()).model_version == original


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
