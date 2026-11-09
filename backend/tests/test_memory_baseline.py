from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.memory_baseline import (
    DATASET,
    Dataset,
    GoldFact,
    extraction_metrics,
    isolated_settings,
    main,
    match_facts,
    recall_metrics,
    run_baseline,
)

from shovel.config.embedding_config import EmbeddingSettings
from shovel.config.settings import AppSettings
from shovel.memory.agent_framework import AgentFrameworkMemoryExtractor
from shovel.memory.domain import MemoryRecord, MemoryScope, MemoryState
from test_hosted_runtime import settings_at


def saved(fact: GoldFact, identifier: str) -> MemoryRecord:
    candidate = fact.candidate()
    return MemoryRecord(
        identifier,
        candidate.kind,
        candidate.text,
        MemoryScope("evaluation"),
        MemoryState.ACTIVE,
        candidate.fingerprint,
        1,
        0,
        1,
        0.5,
        subject="user",
        predicate=candidate.predicate,
        object=candidate.object,
        occurred_at=candidate.occurred_at,
        episode_time_precision="day",
    )


def test_scoring_counts_duplicate_and_wrong_predictions_without_model_judging() -> None:
    fact = GoldFact(key="tea", text="Prefers tea", predicate="prefers", object="jasmine tea")
    records = [saved(fact, "first"), saved(fact, "duplicate"), replace(saved(fact, "wrong"), object="coffee")]
    assert match_facts([fact], records) == {"tea": "first"}
    rows = [
        {"expected": 1, "predicted": 3, "matched": 1, "error": None},
        {"expected": 0, "predicted": 1, "matched": 0, "error": None},
        {"expected": 1, "predicted": 0, "matched": 0, "error": "ValueError"},
    ]
    metrics = extraction_metrics(rows)
    assert (metrics["true_positive"], metrics["false_positive"], metrics["false_negative"]) == (1, 3, 1)
    assert metrics["precision"] == 0.25
    assert metrics["recall"] == 0.5
    assert metrics["f1"] == pytest.approx(1 / 3)
    assert metrics["negative_case_false_positive_rate"] == 1
    assert metrics["error_count"] == 1
    assert recall_metrics(["tea", "missing"], ["other", "tea", "tea"], 5) == {
        "recall_at_k": 0.5,
        "precision_at_k": 0.2,
        "reciprocal_rank": 0.5,
    }
    assert recall_metrics(["missing"], [], 5)["recall_at_k"] == 0


def test_gold_matching_requires_kind_predicate_value_and_explicit_episode_date() -> None:
    fact = GoldFact(
        key="tea", text="Prefers tea", predicate="prefers", object="jasmine tea", aliases=["茉莉花茶"]
    )
    assert fact.matches(replace(saved(fact, "tea"), object="茉莉花茶"))
    assert not fact.matches(replace(saved(fact, "tea"), predicate="dislikes"))
    assert not fact.matches(replace(saved(fact, "tea"), subject="someone else"))
    episode = GoldFact(
        key="episode", text="Finished Cedar", kind="episodic", object="Cedar", occurred_on="2026-10-12"
    )
    assert episode.matches(saved(episode, "episode"))
    assert not episode.matches(replace(saved(episode, "episode"), occurred_at=None))
    assert not episode.matches(replace(saved(episode, "episode"), episode_time_precision="instant"))


def test_live_settings_isolate_all_runtime_paths_without_environment_or_extension_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = settings_at(tmp_path / "original")
    original.agent.enable_python_extensions = True
    original.memory.review_required = True
    monkeypatch.setenv("SHOVEL_DATABASE__URL", "sqlite+aiosqlite:///never-open-user.db")
    monkeypatch.setenv("SHOVEL_MEMORY__INDEX_PATH", str(tmp_path / "never-open-user-memory"))
    monkeypatch.setenv("SHOVEL_AGENT__WORKSPACE_PATH", str(tmp_path / "never-open-user-workspace"))
    root = tmp_path / "evaluation"
    settings = isolated_settings(original, root)
    assert settings.database.url == f"sqlite+aiosqlite:///{(root / 'memory.db').as_posix()}"
    assert settings.agent.workspace == root / "workspace"
    assert settings.agent.enable_python_extensions is False
    assert Path(settings.zvec.knowledge_path) == root / "knowledge"
    assert Path(settings.zvec.log_dir) == root / "logs"
    assert settings.memory.index_path == root / "memory"
    assert settings.temp_path == root / "temp"
    assert settings.memory.review_required is False
    assert settings.model == original.model
    assert settings.memory.embedder == original.memory.embedder
    assert original.memory.review_required is True
    assert original.agent.enable_python_extensions is True


@pytest.mark.parametrize("change", ["duplicate", "unknown"])
def test_dataset_rejects_ambiguous_gold_identities_and_unknown_query_references(change: str) -> None:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    if change == "duplicate":
        payload["distractors"][0]["key"] = payload["cases"][0]["expected"][0]["key"]
    else:
        payload["cases"][0]["queries"][0]["expected"] = ["not-a-gold-fact"]
    with pytest.raises(ValueError):
        Dataset.model_validate(payload)


@pytest.mark.parametrize("invalid_output", [False, True])
async def test_baseline_exercises_real_storage_and_distinguishes_model_errors_from_empty_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_output: bool
) -> None:
    dataset = Dataset.model_validate_json(DATASET.read_bytes())
    sources = {case.source: case for case in dataset.cases}
    calls = []

    async def extract(_self: AgentFrameworkMemoryExtractor, source: str, _related: Any = ()) -> list[Any]:
        calls.append(source)
        if invalid_output and sources[source].id == "english-profile":
            raise ValueError("Synthetic invalid structured output")
        return [replace(fact.candidate(), evidence=source) for fact in sources[source].expected]

    class Embedder:
        model_version = "evaluation-v1"
        dimension = 4

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        async def aclose(self) -> None:
            pass

    async def no_http(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Offline evaluation tests must never contact model providers.")

    monkeypatch.setattr(AgentFrameworkMemoryExtractor, "extract", extract)
    monkeypatch.setattr("shovel.runtime.memory.build_embedder", lambda *_args, **_kwargs: Embedder())
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    settings = settings_at(tmp_path / "original")
    settings.memory.embedder = EmbeddingSettings(provider="openai", model="fixture")
    report = await run_baseline(settings, tmp_path / "evaluation", repeats=1, include_ablation=True)
    result = report["repeats"][0]
    metrics = result["extraction"]["metrics"]
    assert report["complete"] is not invalid_output
    assert metrics["false_positive"] == 0
    assert metrics["false_negative"] == (2 if invalid_output else 0)
    assert metrics["error_count"] == int(invalid_output)
    assert metrics["recall"] == (0.75 if invalid_output else 1)
    assert len(calls) == sum(row["extractor_eligible"] for row in result["extraction"]["cases"])
    for phase in ("gold_recall", "end_to_end_recall"):
        assert set(result[phase]["metrics"]) == {"keyword_profile", "vector", "hybrid"}
        assert all(0 <= row["recall_at_k"] <= 1 for row in result[phase]["queries"])
        variants = result[phase]["ablation"]
        assert len(variants) == 10
        assert all(
            row["serialized_chars"] <= variant["context_max_chars"]
            for variant in variants
            for row in variant["queries"]
        )
        assert all(variant["unrelated_queries"] for variant in variants)
        assert all(0 <= variant["unrelated_query_false_positive_rate"] <= 1 for variant in variants)
    encoded = json.dumps(report)
    assert settings.model.security_key not in encoded
    assert settings.model.base_endpoint not in encoded
    assert str(tmp_path) not in encoded


def test_cli_requires_explicit_live_opt_in_before_reading_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SHOVEL_RUN_LIVE_TESTS", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["memory_baseline", "--config", "never-read.toml", "--output", "unused.json"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "SHOVEL_RUN_LIVE_TESTS=1" in capsys.readouterr().err


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1", reason="Explicit live endpoint opt-in required"
)
async def test_live_memory_extraction_and_recall_baseline(tmp_path: Path) -> None:
    path = os.environ.get("SHOVEL_LIVE_CONFIG")
    assert path, "Set SHOVEL_LIVE_CONFIG to an explicitly selected settings.toml."
    report = await run_baseline(AppSettings.load(Path(path)), tmp_path)
    output = Path(os.environ.get("SHOVEL_MEMORY_BASELINE_REPORT", str(tmp_path / "memory-baseline.json")))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert report["complete"], "Structured model output errors are recorded in the baseline report."
