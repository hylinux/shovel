from __future__ import annotations

import os
import sqlite3
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shovel.cli import app
from shovel.cli.commands import init as init_command
from shovel.config.settings import AppSettings
from shovel.runtime.agent import model_endpoint
from shovel.services.health import HealthResult, HealthStatus
from shovel.vector.store import init_vector_store, open_collection


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in os.environ:
        if name.startswith("SHOVEL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(init_command, "_STEP_PAUSE_SECONDS", 0)
    monkeypatch.setattr(init_command, "init_vector_store", lambda settings: [])
    monkeypatch.setattr(
        init_command,
        "check_model",
        lambda settings: HealthResult(HealthStatus.SKIPPED, "", "Offline test; no model requests."),
    )
    return tmp_path / ".shovel"


def _answers(**overrides: str) -> str:
    values = {
        "model_provider": "1",
        "model_endpoint": "https://chat.example.test",
        "model_name": "chat-deployment",
        "model_version": "2025-04-01-preview",
        "model_key": "",
        "knowledge": "Knowledge_42",
        "embedding_provider": "1",
        "embedding_endpoint": "https://embedding.example.test",
        "embedding_model": "embedding-deployment",
        "dimension": "",
        "embedding_key": "",
        "embedding_version": "2025-04-01-preview",
        "memory": "Memory_42",
    }
    assert overrides.keys() <= values.keys()
    values.update(overrides)
    return "\n" + "\n".join(values.values()) + "\n"


def test_init_wizard_persists_inputs_defaults_and_independent_memory_copies(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_command, "init_vector_store", init_vector_store)
    result = CliRunner().invoke(
        app,
        ["init"],
        input=_answers(model_key="chat-private-key", embedding_key="embedding-private-key"),
    )
    assert result.exit_code == 0, result.output
    assert "chat-private-key" not in result.output
    assert "embedding-private-key" not in result.output
    config = profile / "config" / "settings.toml"
    text = config.read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if line.startswith("timeout_seconds =")] == [
        "timeout_seconds = 300"
    ] * 4
    raw = tomllib.loads(text)
    assert raw["model"] == raw["memory"]["llm"]
    assert raw["embedding"] == raw["memory"]["embedder"]
    assert raw["model"]["api_key"] == "chat-private-key"
    assert raw["embedding"]["api_key"] == "embedding-private-key"
    assert raw["model"]["base_endpoint"] == "https://chat.example.test"
    assert raw["model"]["model_name"] == "chat-deployment"
    assert raw["embedding"]["base_url"] == "https://embedding.example.test"
    assert raw["embedding"]["model"] == "embedding-deployment"
    assert raw["model"]["max_input_chars"] == 240000
    assert raw["model"]["max_output_tokens"] == 32768
    assert raw["model"]["timeout_seconds"] == 300
    assert raw["agent"]["max_tool_iterations"] == 5
    assert raw["performance"]["max_local_model_requests"] == 3
    assert raw["embedding"]["dimensions"] == raw["zvec"]["dim"] == raw["memory"]["dim"] == 1024
    assert raw["embedding"]["batch_size"] == 8
    assert raw["embedding"]["timeout_seconds"] == 300
    assert raw["memory"]["max_items"] == 20
    assert raw["memory"]["history_messages"] == 10
    assert raw["memory"]["collection"] == "Memory_42"
    assert not {"agent_host", "agent_port", "document"} & raw.keys()
    settings = AppSettings.load(config)
    assert settings.agent.workspace == profile / "workspaces" / "default"
    assert Path(settings.zvec.knowledge_path) == profile / "knowledge" / "Knowledge_42"
    assert settings.zvec.chunk_collection == "Knowledge_42"
    collection = open_collection(settings.zvec.knowledge_path)
    try:
        assert collection.schema.name == "Knowledge_42"
    finally:
        collection.close()
    assert settings.memory.index_path == profile / "memory" / "Memory_42"
    assert settings.memory.index_path.is_dir()
    assert any(Path(settings.zvec.knowledge_path).iterdir())
    assert settings.database.url == f"sqlite+aiosqlite:///{(profile / 'data' / 'shovel.db').as_posix()}"
    with sqlite3.connect(profile / "data" / "shovel.db") as database:
        assert database.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0
    for directory in ("skills", "functions", "pipeline"):
        assert (settings.agent.workspace / directory).is_dir()
    settings.model.model_name = "changed"
    settings.embedding.model = "changed"
    assert settings.memory.llm.model_name == "chat-deployment"
    assert settings.memory.embedder.model == "embedding-deployment"


@pytest.mark.parametrize(
    "model_choice,model_provider,embedding_choice,embedding_provider",
    [
        ("1", "azure_openai", "1", "azure_openai"),
        ("2", "openai", "2", "openai"),
        ("3", "deepseek", "3", "qianwen"),
        ("4", "qianwen", "4", "ollama"),
        ("5", "ollama", "4", "ollama"),
    ],
)
def test_init_provider_choices_optional_keys_and_dimension_override(
    profile: Path, model_choice: str, model_provider: str, embedding_choice: str, embedding_provider: str
) -> None:
    result = CliRunner().invoke(
        app,
        ["init"],
        input=_answers(
            model_provider=model_choice,
            model_endpoint="http://127.0.0.1:11434"
            if model_provider == "ollama"
            else "https://chat.example.test",
            model_name="shovel-chat-fast" if model_provider == "ollama" else "chat-deployment",
            model_version="2025-04-01-preview" if model_provider == "azure_openai" else "",
            embedding_provider=embedding_choice,
            embedding_version="2025-04-01-preview" if embedding_provider == "azure_openai" else "",
            dimension="3072",
        ),
    )
    assert result.exit_code == 0, result.output
    raw = tomllib.loads((profile / "config" / "settings.toml").read_text(encoding="utf-8"))
    assert raw["model"]["model_provider_type"] == model_provider
    assert raw["embedding"]["provider"] == embedding_provider
    assert raw["zvec"]["dim"] == raw["memory"]["dim"] == 3072
    assert raw["model"]["api_key"] == raw["embedding"]["api_key"] == ""
    if embedding_provider == "ollama":
        assert "dimensions" not in raw["embedding"]
        assert "dimensions" not in raw["memory"]["embedder"]
        assert "Ollama does not accept a dimensions parameter" in result.output
    else:
        assert raw["embedding"]["dimensions"] == raw["memory"]["embedder"]["dimensions"] == 3072
    assert raw["model"] == raw["memory"]["llm"]
    assert raw["embedding"] == raw["memory"]["embedder"]
    if model_provider == "ollama":
        settings = AppSettings.load(profile / "config" / "settings.toml")
        assert settings.model.base_endpoint == "http://127.0.0.1:11434"
        assert settings.model.model_name == settings.memory.llm.model_name == "shovel-chat-fast"
        assert settings.model.api_version == settings.memory.llm.api_version == ""
        assert model_endpoint(settings.model) == "http://127.0.0.1:11434/v1"
        assert model_endpoint(settings.memory.llm) == "http://127.0.0.1:11434/v1"


def test_init_reprompts_invalid_choices_endpoints_names_dimensions_and_azure_versions(profile: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["init"],
        input=_answers(
            model_provider="0\nnot-a-number\n6\n1",
            model_endpoint=(
                "   \nnot-a-url\nhttps://chat.example.test:invalid\n"
                "https://user:password@chat.example.test\n"
                "https://chat.example.test/openai/deployments/test\nhttps://chat.example.test"
            ),
            model_name="   \nchat-deployment",
            model_version="\n2025-04-01-preview",
            knowledge="../escape\nwith space\nname-with-dash\n\u77e5\u8bc6\nKnowledge_42",
            dimension="0\n-1\nnot-a-number\n1024",
            embedding_endpoint="https://embedding.example.test?key=invalid\nhttps://embedding.example.test",
            embedding_version="\n2025-04-01-preview",
            memory="..\ninvalid.name\nMemory_42",
        ),
    )
    assert result.exit_code == 0, result.output
    settings = AppSettings.load(profile / "config" / "settings.toml")
    assert settings.model.base_endpoint == "https://chat.example.test"
    assert settings.model.api_version == settings.embedding.api_version == "2025-04-01-preview"
    assert Path(settings.zvec.knowledge_path).name == "Knowledge_42"
    assert settings.memory.collection == "Memory_42"
    assert settings.memory.dim == settings.zvec.dim == settings.embedding.dimensions == 1024
    assert "Azure OpenAI requires an API version" in result.output
    assert "ASCII letters" in result.output
    assert "positive integer" in result.output


@pytest.mark.parametrize(
    "input_text",
    [
        "",
        "\n1\n",
        "\n1\nhttps://chat.example.test\n",
        _answers(model_key="chat-private-key", embedding_key="embedding-private-key").removesuffix(
            "Memory_42\n"
        ),
    ],
)
def test_init_cancelled_input_does_not_save_partial_configuration(profile: Path, input_text: str) -> None:
    result = CliRunner().invoke(app, ["init"], input=input_text)
    assert result.exit_code != 0
    assert "Initialization cancelled" in result.output
    assert "unexpected error" not in result.output
    assert "private-key" not in result.output
    assert not profile.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved filenames")
def test_init_rejects_reserved_store_names_on_windows(profile: Path) -> None:
    result = CliRunner().invoke(
        app, ["init"], input=_answers(knowledge="CON\nKnowledge_42", memory="AUX\nMemory_42")
    )
    assert result.exit_code == 0, result.output
    settings = AppSettings.load(profile / "config" / "settings.toml")
    assert Path(settings.zvec.knowledge_path).name == "Knowledge_42"
    assert settings.memory.collection == "Memory_42"
    assert "reserved filenames are not allowed" in result.output


def test_init_no_input_creates_unconfigured_profile_without_persisting_environment(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOVEL_MODEL__MAX_OUTPUT_TOKENS", "4096")
    result = CliRunner().invoke(app, ["init", "--no-input"])
    assert result.exit_code == 0, result.output
    settings = AppSettings.load(profile / "config" / "settings.toml")
    assert "document" not in settings.model_dump()
    assert not settings.model.is_configured and not settings.embedding.is_configured
    assert not settings.memory.llm.is_configured and not settings.memory.embedder.is_configured
    assert settings.model.max_output_tokens == settings.memory.llm.max_output_tokens == 32768
    assert (
        settings.zvec.dim
        == settings.memory.dim
        == settings.embedding.dimensions
        == settings.memory.embedder.dimensions
        == 1024
    )
    assert Path(settings.zvec.knowledge_path) == profile / "knowledge" / "shovel_chunk"
    assert settings.zvec.chunk_collection == "shovel_chunk"
    assert settings.memory.index_path == profile / "memory" / "shovel_memory"


def test_init_existing_configuration_is_not_prompted_or_rewritten(profile: Path) -> None:
    path = profile / "config" / "settings.toml"
    settings = AppSettings.from_file_data(
        {
            "zvec": {"dim": 512, "knowledge_path": str(profile / "knowledge" / "existing")},
            "memory": {"dim": 512, "index_path": profile / "memory" / "existing"},
        }
    )
    settings.save(path)
    path.write_text(
        "# Keep my comments and values.\n" + path.read_text() + '\n[document]\nroot_dir = "legacy-root"\n',
        encoding="utf-8",
    )
    before = path.read_bytes()
    result = CliRunner().invoke(app, ["init"], input="\n")
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before
    assert AppSettings.load(path).model.max_output_tokens == 2048
    assert "Model provider" not in result.output


def test_init_invalid_existing_configuration_is_not_overwritten(profile: Path) -> None:
    path = profile / "config" / "settings.toml"
    path.parent.mkdir(parents=True)
    path.write_text("invalid TOML", encoding="utf-8")
    result = CliRunner().invoke(app, ["init"], input="\n")
    assert result.exit_code != 0
    assert path.read_text(encoding="utf-8") == "invalid TOML"
    assert "Model provider" not in result.output
