"""Opt-in real-provider Memory evaluation using only fixed synthetic assertions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shovel.config.agent_config import AgentSettings
from shovel.config.database_config import DatabaseSettings
from shovel.config.platform_config import PlatformSettings
from shovel.config.settings import AppSettings
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.model.runtime import ChatMessage, Conversation
from shovel.memory.domain import (
    EXTRACTION_VERSION,
    MemoryCandidate,
    MemoryRecord,
    MemoryType,
    Provenance,
    eligible_source,
    normalize,
)
from shovel.runtime.service import Runtime, create_runtime
from shovel.services.resource_service import ResourceService

DATASET = Path(__file__).resolve().parents[1] / "backend" / "tests" / "fixtures" / "memory_cases.json"
RETRIEVAL_SOURCE = (
    Path(__file__).resolve().parents[1] / "backend" / "src" / "shovel" / "memory" / "service.py"
)
RECALL_PROBES_VERSION = "memory-unrelated-recall-v1"
UNRELATED_QUERIES = (
    "What is my shoe size?",
    "Which airline do I use for business travel?",
    "What is my favorite pizza topping?",
    "Which database do I use for project Maple?",
    "When did I finish the Birch offline evaluation?",
)


class GoldFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    text: str
    kind: Literal["semantic", "episodic"] = "semantic"
    predicate: str = "knows_about"
    object: str
    topic: str = ""
    aliases: list[str] = Field(default_factory=list)
    occurred_on: date | None = None

    def candidate(self) -> MemoryCandidate:
        return MemoryCandidate(
            self.text,
            kind=MemoryType(self.kind),
            predicate=self.predicate,
            object=self.object,
            topic=self.topic,
            confidence=1,
            evidence=self.text,
            occurred_at=(
                int(datetime.combine(self.occurred_on, datetime.min.time(), tzinfo=UTC).timestamp())
                if self.occurred_on
                else None
            ),
            time_precision="day",
        )

    def matches(self, record: MemoryRecord) -> bool:
        if record.kind.value != self.kind:
            return False
        values = {normalize(value) for value in (self.object, *self.aliases)}
        if self.kind == "semantic":
            return (
                normalize(record.subject) == "user"
                and record.predicate == self.predicate
                and normalize(record.object) in values
            )
        return (
            record.occurred_at == self.candidate().occurred_at
            and record.episode_time_precision == "day"
            and any(value in normalize(record.text) for value in values)
        )


class RecallQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    expected: list[str] = Field(min_length=1)


class ExtractionCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str
    expected: list[GoldFact]
    queries: list[RecallQuery]


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    cases: list[ExtractionCase] = Field(min_length=1)
    distractors: list[GoldFact]

    @model_validator(mode="after")
    def valid_keys(self) -> Dataset:
        keys = [fact.key for case in self.cases for fact in case.expected]
        all_keys = [*keys, *(fact.key for fact in self.distractors)]
        if len(set(all_keys)) != len(all_keys) or len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Dataset case and fact identities must be unique.")
        if not keys or not any(case.queries for case in self.cases):
            raise ValueError("Dataset requires positive extraction and recall cases.")
        if any(set(query.expected) - set(keys) for case in self.cases for query in case.queries):
            raise ValueError("Recall query references an unknown gold fact.")
        return self


def isolated_settings(configured: AppSettings, root: Path) -> AppSettings:
    return configured.model_copy(
        update={
            "database": DatabaseSettings(url=f"sqlite+aiosqlite:///{(root / 'memory.db').as_posix()}"),
            "temp_path": root / "temp",
            "agent": AgentSettings(workspace_path=root / "workspace"),
            "zvec": configured.zvec.model_copy(
                update={"knowledge_path": str(root / "knowledge"), "log_dir": str(root / "logs")}
            ),
            "memory": configured.memory.model_copy(
                update={"index_path": root / "memory", "review_required": False}
            ),
            "platform": PlatformSettings(),
        }
    )


@asynccontextmanager
async def baseline_runtime(settings: AppSettings) -> AsyncIterator[Runtime]:
    context = create_async_database_engine(settings.database.url)
    engine = await anext(context)
    sessions = create_async_session_factory(engine)
    managed = create_runtime(settings, engine, sessions, ResourceService(sessions))
    try:
        yield await anext(managed)
    finally:
        await managed.aclose()
        await context.aclose()


def match_facts(expected: Sequence[GoldFact], records: Sequence[MemoryRecord]) -> dict[str, str]:
    matched: dict[str, str] = {}
    for record in records:
        fact = next((fact for fact in expected if fact.key not in matched and fact.matches(record)), None)
        if fact is not None:
            matched[fact.key] = record.id
    return matched


def extraction_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    true_positive = sum(row["matched"] for row in rows)
    predicted = sum(row["predicted"] for row in rows)
    expected = sum(row["expected"] for row in rows)
    negatives = [row for row in rows if row["expected"] == 0]
    precision = true_positive / predicted if predicted else 0.0
    recall = true_positive / expected if expected else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": predicted - true_positive,
        "false_negative": expected - true_positive,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "negative_case_false_positive_rate": (
            sum(row["predicted"] > 0 for row in negatives) / len(negatives) if negatives else None
        ),
        "error_count": sum(row["error"] is not None for row in rows),
    }


def recall_metrics(expected: Sequence[str], ranked: Sequence[str], top_k: int) -> dict[str, float]:
    selected = list(ranked[:top_k])
    found = set(expected).intersection(selected)
    return {
        "recall_at_k": len(found) / len(set(expected)),
        "precision_at_k": len(found) / top_k,
        "reciprocal_rank": next(
            (1 / position for position, key in enumerate(selected, 1) if key in expected), 0.0
        ),
    }


async def extract_cases(runtime: Runtime, dataset: Dataset) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    identities: dict[str, str] = {}
    for case in dataset.cases:
        async with runtime.sessions() as session:
            conversation = Conversation()
            session.add(conversation)
            await session.flush()
            message = ChatMessage(
                conversation_id=conversation.id, role="user", content=case.source, sequence=1
            )
            session.add(message)
            await session.commit()
        started = perf_counter()
        records: list[MemoryRecord] = []
        error = None
        try:
            records = await runtime.memory.application.extract(
                case.source,
                Provenance("user_message", conversation.id, message.id, extractor_version=EXTRACTION_VERSION),
            )
        except ValueError as exc:
            # Invalid structured model output is an evaluated failure, never an empty-success response.
            error = type(exc).__name__
        matched = match_facts(case.expected, records)
        identities.update({identifier: key for key, identifier in matched.items()})
        rows.append(
            {
                "case": case.id,
                "expected": len(case.expected),
                "predicted": len(records),
                "matched": len(matched),
                "matched_keys": sorted(matched),
                "error": error,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "extractor_eligible": eligible_source(case.source),
                "records": [
                    {
                        "kind": record.kind.value,
                        "predicate": record.predicate,
                        "object": record.object,
                        "text": record.text,
                        "occurred_at": record.occurred_at,
                    }
                    for record in records
                ],
            }
        )
    return rows, identities


async def seed(runtime: Runtime, facts: Sequence[GoldFact]) -> dict[str, str]:
    identities = {}
    for fact in facts:
        records = await runtime.memory.repository.consolidate(
            runtime.memory.owner, [fact.candidate()], Provenance("manual")
        )
        if len(records) != 1:
            raise RuntimeError("The fixed recall corpus could not be seeded.")
        identities[records[0].id] = fact.key
    return identities


async def recall_cases(
    runtime: Runtime, dataset: Dataset, identities: dict[str, str], top_k: int
) -> dict[str, Any]:
    started = perf_counter()
    await runtime.memory.rebuild()
    index_ms = round((perf_counter() - started) * 1000, 2)
    status = await runtime.memory.status()
    if status["index_state"] != "ready":
        raise RuntimeError("The evaluation requires a ready semantic index, not degraded keyword recall.")
    rows: list[dict[str, Any]] = []
    for case in dataset.cases:
        for query in case.queries:
            for mode in ("keyword_profile", "vector", "hybrid"):
                started = perf_counter()
                diagnostics: dict[str, Any] = {}
                if mode == "vector":
                    assert runtime.memory.index is not None
                    hits = await runtime.memory.index.search(query.text, runtime.memory.owner, top_k)
                    identifiers = [
                        hit.id for hit in hits if hit.similarity >= runtime.settings.memory.min_similarity
                    ]
                    diagnostics = {
                        "raw_hits": [
                            {
                                "key": identities.get(hit.id, "unmatched"),
                                "similarity": hit.similarity,
                            }
                            for hit in hits
                        ],
                        "raw_metrics": recall_metrics(
                            query.expected,
                            [identities.get(hit.id, "unmatched") for hit in hits],
                            top_k,
                        ),
                    }
                else:
                    items = await runtime.memory.application.retrieve(
                        query.text,
                        "new-evaluation-session",
                        use_index=mode == "hybrid",
                        record_access=False,
                    )
                    identifiers = [str(item["id"]) for item in items[:top_k]]
                    diagnostics = {
                        "returned_items": len(items),
                        "serialized_chars": len(json.dumps(items, ensure_ascii=False)),
                    }
                ranked = [identities.get(identifier, "unmatched") for identifier in identifiers]
                rows.append(
                    {
                        "case": case.id,
                        "query": query.text,
                        "mode": mode,
                        "expected": query.expected,
                        "ranked": ranked,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        **diagnostics,
                        **recall_metrics(query.expected, ranked, top_k),
                    }
                )
    metrics = {}
    for mode in ("keyword_profile", "vector", "hybrid"):
        selected = [row for row in rows if row["mode"] == mode]
        metrics[mode] = {
            key: sum(row[key] for row in selected) / len(selected)
            for key in ("recall_at_k", "precision_at_k", "reciprocal_rank", "latency_ms")
        }
    vector_rows = [row for row in rows if row["mode"] == "vector"]
    metrics["vector"]["raw_recall_at_k"] = sum(
        row["raw_metrics"]["recall_at_k"] for row in vector_rows
    ) / len(vector_rows)
    return {"index_ms": index_ms, "metrics": metrics, "queries": rows}


async def recall_ablation(
    runtime: Runtime, dataset: Dataset, identities: dict[str, str], top_k: int
) -> list[dict[str, Any]]:
    application = runtime.memory.application
    original = application.min_similarity, application.max_chars
    profile_ids = {
        record.id for record in await runtime.memory.repository.profile(runtime.memory.owner, limit=100)
    }
    queries = [query for case in dataset.cases for query in case.queries]
    rows: list[dict[str, Any]] = []
    try:
        for threshold in (0.35, 0.4, 0.45, 0.5, 0.55):
            for budget in (2000, 4000):
                application.min_similarity, application.max_chars = threshold, budget
                positive_rows: list[dict[str, Any]] = []
                unrelated_rows: list[dict[str, Any]] = []
                for query in queries:
                    diagnostics: dict[str, int] = {}
                    items = await application.retrieve(
                        query.text,
                        "new-evaluation-session",
                        record_access=False,
                        diagnostics=diagnostics,
                    )
                    ranked = [identities.get(str(item["id"]), "unmatched") for item in items]
                    positive_rows.append(
                        {
                            "query": query.text,
                            "ranked": ranked,
                            **recall_metrics(query.expected, ranked, top_k),
                            **diagnostics,
                        }
                    )
                for text in UNRELATED_QUERIES:
                    items = await application.retrieve(text, "new-evaluation-session", record_access=False)
                    non_profile = [item for item in items if item["id"] not in profile_ids]
                    unrelated_rows.append(
                        {
                            "query": text,
                            "ranked_non_profile": [
                                identities.get(str(item["id"]), "unmatched") for item in non_profile
                            ],
                            "false_positive": bool(non_profile),
                        }
                    )
                rows.append(
                    {
                        "min_similarity": threshold,
                        "context_max_chars": budget,
                        "metrics": {
                            key: sum(row[key] for row in positive_rows) / len(positive_rows)
                            for key in ("recall_at_k", "precision_at_k", "reciprocal_rank")
                        },
                        "unrelated_query_false_positive_rate": (
                            sum(row["false_positive"] for row in unrelated_rows) / len(unrelated_rows)
                        ),
                        "queries": positive_rows,
                        "unrelated_queries": unrelated_rows,
                    }
                )
    finally:
        application.min_similarity, application.max_chars = original
    return rows


async def run_baseline(
    configured: AppSettings,
    root: Path,
    *,
    dataset_path: Path = DATASET,
    repeats: int = 2,
    top_k: int = 5,
    include_ablation: bool = False,
) -> dict[str, Any]:
    if not 1 <= repeats <= 5 or not 1 <= top_k <= 20:
        raise ValueError("Use 1-5 repeats and a top_k between 1 and 20.")
    model = configured.memory.llm if configured.memory.llm.is_configured else configured.model
    if not model.is_configured or not configured.memory.embedder.is_configured:
        raise ValueError("Configure a Memory extraction model and [memory.embedder].")
    dataset_bytes = await asyncio.to_thread(dataset_path.read_bytes)
    dataset = Dataset.model_validate_json(dataset_bytes)
    gold = [fact for case in dataset.cases for fact in case.expected]
    report: dict[str, Any] = {
        "schema_version": 1,
        "created_at": int(datetime.now(UTC).timestamp()),
        "dataset_version": dataset.version,
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "evaluator_sha256": hashlib.sha256(await asyncio.to_thread(Path(__file__).read_bytes)).hexdigest(),
        "retrieval_sha256": hashlib.sha256(await asyncio.to_thread(RETRIEVAL_SOURCE.read_bytes)).hexdigest(),
        "recall_probes_version": RECALL_PROBES_VERSION if include_ablation else None,
        "extractor_version": EXTRACTION_VERSION,
        "python": platform.python_version(),
        "shovel_version": version("shovel"),
        "package_versions": {name: version(name) for name in ("agent-framework-core", "openai", "zvec")},
        "model": {
            "provider": model.model_provider_type.value,
            "name": model.model_name,
            "api_version": model.api_version,
            "max_input_chars": model.max_input_chars,
            "max_output_tokens": model.max_output_tokens,
            "timeout_seconds": model.timeout_seconds,
        },
        "memory": configured.memory.model_dump(
            include={"dim", "max_items", "context_max_chars", "min_similarity", "min_confidence"}
        ),
        "top_k": top_k,
        "review_required": {"configured": configured.memory.review_required, "effective": False},
        "repeats": [],
    }
    for repeat in range(repeats):
        async with baseline_runtime(
            isolated_settings(configured, root / str(repeat) / "extracted")
        ) as runtime:
            extraction, identities = await extract_cases(runtime, dataset)
            identities.update(await seed(runtime, dataset.distractors))
            end_to_end = await recall_cases(runtime, dataset, identities, top_k)
            if include_ablation:
                end_to_end["ablation"] = await recall_ablation(runtime, dataset, identities, top_k)
            assert runtime.memory.index is not None
            report["embedding_model_version"] = runtime.memory.index.model_version
        async with baseline_runtime(isolated_settings(configured, root / str(repeat) / "gold")) as runtime:
            identities = await seed(runtime, [*gold, *dataset.distractors])
            recall = await recall_cases(runtime, dataset, identities, top_k)
            if include_ablation:
                recall["ablation"] = await recall_ablation(runtime, dataset, identities, top_k)
        report["repeats"].append(
            {
                "extraction": {"metrics": extraction_metrics(extraction), "cases": extraction},
                "gold_recall": recall,
                "end_to_end_recall": end_to_end,
            }
        )
    report["complete"] = all(
        repeat["extraction"]["metrics"]["error_count"] == 0 for repeat in report["repeats"]
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--ablation", action="store_true", help="Compare recall thresholds and budgets.")
    args = parser.parse_args()
    if os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1":
        parser.error("Set SHOVEL_RUN_LIVE_TESTS=1 to authorize calls to the selected model providers.")
    configured = AppSettings.load(args.config)
    with TemporaryDirectory(prefix="shovel-memory-baseline-") as directory:
        report = asyncio.run(
            run_baseline(configured, Path(directory), repeats=args.repeats, include_ablation=args.ablation)
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"complete": report["complete"], "report": str(args.output)}, ensure_ascii=False))
    if not report["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
