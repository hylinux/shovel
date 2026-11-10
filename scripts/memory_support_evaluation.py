"""Opt-in paired retrieval and real Main Agent answers on a frozen synthetic corpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import BaseModel, ConfigDict
from scripts.memory_baseline import DATASET, Dataset, GoldFact, baseline_runtime, isolated_settings, seed

from shovel.config.settings import AppSettings
from shovel.memory.domain import MemoryRecord, MemoryScope, MemoryType, VectorHit
from shovel.runtime.schemas import MessageInput

CASES = DATASET.with_name("memory_support_cases.json")
SOURCE_ROOT = DATASET.parents[3]
ANSWER_INSTRUCTIONS = """
For this synthetic evaluation, use only the provided Memory facts, not general knowledge.
Return only JSON: {"status": "supported" or "unknown", "answers": [strings]}.
For supported answers return only the recorded object values, or ISO dates for event dates.
If the facts do not support the requested entity AND relation, use unknown and an empty list.
Background profile facts are not answers to unrelated questions. Do not call tools.
"""


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    expected: list[str]


class SupportDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    facts: list[GoldFact]
    queries: list[Question]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["supported", "unknown"]
    answers: list[str]


def source_fingerprints() -> dict[str, str]:
    files = [
        SOURCE_ROOT / "backend" / "src" / "shovel" / "memory" / name
        for name in ("service.py", "sqlite_repository.py", "relevance.py")
    ]
    files.extend(
        SOURCE_ROOT / "backend" / "src" / "shovel" / "runtime" / name
        for name in ("agent.py", "agent_instructions.py")
    )
    files.append(Path(__file__))
    return {
        str(path.relative_to(SOURCE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }


def answer_correct(answer: Answer, question: Question, facts: dict[str, GoldFact]) -> bool:
    if not question.expected:
        return answer.status == "unknown" and not answer.answers
    expected = [facts[key] for key in question.expected]
    values = [
        {str(fact.occurred_on)}
        if fact.occurred_on
        else {value.casefold() for value in (fact.object, *fact.aliases)}
        for fact in expected
    ]
    return (
        answer.status == "supported"
        and len(answer.answers) == len(expected)
        and all(any(value.casefold() in accepted for value in answer.answers) for accepted in values)
    )


class FrozenIndex:
    def __init__(self, snapshot: dict[str, Any], scope: MemoryScope) -> None:
        self.snapshot, self.scope = snapshot, scope
        self.model_version: str = snapshot["embedding_model_version"]

    async def search(self, query: str, scope: MemoryScope, limit: int) -> list[VectorHit]:
        if (scope.workspace_id, scope.user_id, scope.agent_id) != (
            self.scope.workspace_id,
            self.scope.user_id,
            self.scope.agent_id,
        ):
            raise ValueError("Frozen evaluation queries must retain their original ownership.")
        return [
            VectorHit(row["id"], MemoryType(row["kind"]), row["revision"], row["similarity"])
            for row in self.snapshot["hits"][query][:limit]
        ]

    async def upsert(self, record: MemoryRecord) -> None:
        raise RuntimeError("The evaluation candidate index is frozen.")

    async def delete(self, identifier: str, kind: str, revision: int) -> None:
        raise RuntimeError("The evaluation candidate index is frozen.")

    async def close(self) -> None:
        pass


class AnswerTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.delegate = httpx.AsyncHTTPTransport()
        self.context: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        self.context = [
            json.loads(message["content"].split("\n", 1)[1])
            for message in body["messages"]
            if isinstance(message.get("content"), str) and message["content"].startswith("Host")
        ]
        body["tool_choice"] = "none"
        headers = [(key, value) for key, value in request.headers.multi_items() if key != "content-length"]
        forwarded = httpx.Request(
            request.method, request.url, headers=headers, json=body, extensions=request.extensions
        )
        return await self.delegate.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self.delegate.aclose()


def prepare_root(configured: AppSettings, root: Path, *, create: bool) -> AppSettings:
    if create:
        if root.exists() and any(root.iterdir()):
            raise ValueError("Create requires a new or empty evaluation directory.")
        root.mkdir(parents=True, exist_ok=True)
    elif not (root / "frozen-memory.json").is_file():
        raise ValueError("Reuse requires an explicitly created frozen evaluation manifest.")
    settings = isolated_settings(configured, root)
    settings.agent.workspace.mkdir(parents=True, exist_ok=True)
    (settings.agent.workspace / "agent-instructions.md").write_text(ANSWER_INSTRUCTIONS, encoding="utf-8")
    return settings


async def evaluate(
    configured: AppSettings, root: Path, *, create: bool, cases_path: Path = CASES
) -> dict[str, Any]:
    source_hashes = await asyncio.to_thread(source_fingerprints)
    dataset_bytes = await asyncio.to_thread(cases_path.read_bytes)
    original_bytes = await asyncio.to_thread(DATASET.read_bytes)
    dataset = SupportDataset.model_validate_json(dataset_bytes)
    original = Dataset.model_validate_json(original_bytes)
    facts = [fact for case in original.cases for fact in case.expected]
    facts.extend([*original.distractors, *dataset.facts])
    by_key = {fact.key: fact for fact in facts}
    if (
        len(by_key) != len(facts)
        or len({query.text for query in dataset.queries}) != len(dataset.queries)
        or not any(query.expected for query in dataset.queries)
        or not any(not query.expected for query in dataset.queries)
        or any(set(q.expected) - by_key.keys() for q in dataset.queries)
    ):
        raise ValueError("Support corpus requires unique facts and known expected keys.")
    manifest = root / "frozen-memory.json"
    dataset_hash = hashlib.sha256(original_bytes + dataset_bytes).hexdigest()
    settings = await asyncio.to_thread(prepare_root, configured, root, create=create)
    async with baseline_runtime(settings) as runtime:
        assert runtime.memory.index is not None
        if create:
            identities = await seed(runtime, facts)
            await runtime.memory.rebuild()
            snapshot: dict[str, Any] = {
                "format": "shovel-frozen-memory-v1",
                "dataset_sha256": dataset_hash,
                "embedding_model_version": runtime.memory.index.model_version,
                "identities": identities,
                "hits": {
                    question.text: [
                        asdict(hit)
                        for hit in await runtime.memory.index.search(
                            question.text, runtime.memory.owner, settings.memory.max_items * 4
                        )
                    ]
                    for question in dataset.queries
                },
            }
        else:
            snapshot = json.loads(await asyncio.to_thread(manifest.read_text, encoding="utf-8"))
            if (
                snapshot["format"] != "shovel-frozen-memory-v1"
                or snapshot["dataset_sha256"] != dataset_hash
                or snapshot["embedding_model_version"] != runtime.memory.index.model_version
            ):
                raise ValueError("Frozen corpus, queries and embedding identity must not change.")
        records = await runtime.memory.repository.list_records(runtime.memory.owner, limit=10000)
        digest = hashlib.sha256(
            json.dumps([asdict(r) for r in sorted(records, key=lambda r: r.id)], sort_keys=True).encode()
        ).hexdigest()
        if create:
            snapshot["records_sha256"] = digest
            await asyncio.to_thread(
                manifest.write_text,
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif snapshot["records_sha256"] != digest:
            raise ValueError("The frozen facts or metadata changed between measurements.")
        runtime.memory.application.index = FrozenIndex(snapshot, runtime.memory.owner)
        transport = AnswerTransport()
        runtime.chat.agent._client = httpx.AsyncClient(transport=transport)
        identities = snapshot["identities"]
        rows: list[dict[str, Any]] = []
        with patch.object(runtime.memory.repository, "touch", new=AsyncMock()):
            for number, question in enumerate(dataset.queries):
                diagnostics: dict[str, int] = {}
                items = await runtime.memory.application.retrieve(
                    question.text, record_access=False, diagnostics=diagnostics
                )
                started = perf_counter()
                response = await runtime.chat.agent.respond(
                    f"support-evaluation-{number}",
                    MessageInput(content=question.text, resource_ids=[]),
                    [],
                )
                answer = Answer.model_validate_json(response.content)
                ranked = [identities[str(item["id"])] for item in items]
                rows.append(
                    {
                        "query": question.text,
                        "expected": question.expected,
                        "ranked": ranked,
                        "candidate_ranked": [
                            identities[hit["id"]] for hit in snapshot["hits"][question.text]
                        ],
                        "supported_ranked": [
                            identities[str(item["id"])]
                            for item in items
                            if item.get("answer_support") == "supported"
                        ],
                        "diagnostics": diagnostics,
                        "model_memory_context": transport.context,
                        "answer": answer.model_dump(),
                        "answer_correct": answer_correct(answer, question, by_key),
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                    }
                )
        positive = [row for row in rows if row["expected"]]
        negative = [row for row in rows if not row["expected"]]
        profile_ids = {
            record.id for record in await runtime.memory.repository.profile(runtime.memory.owner, limit=4)
        }
        profile_keys = {identities[identifier] for identifier in profile_ids}
        supported_count = sum(len(row["supported_ranked"]) for row in rows)
        supported_correct = sum(len(set(row["expected"]) & set(row["supported_ranked"])) for row in rows)
        current = await runtime.memory.repository.list_records(runtime.memory.owner, limit=10000)
        if (
            hashlib.sha256(
                json.dumps([asdict(r) for r in sorted(current, key=lambda r: r.id)], sort_keys=True).encode()
            ).hexdigest()
            != digest
        ):
            raise RuntimeError("The evaluation changed its frozen Memory corpus.")
        if source_hashes != await asyncio.to_thread(source_fingerprints):
            raise RuntimeError("Evaluation source code changed during measurement.")
        return {
            "dataset_version": dataset.version,
            "dataset_sha256": dataset_hash,
            "records_sha256": digest,
            "frozen_hits_sha256": hashlib.sha256(
                json.dumps(snapshot["hits"], sort_keys=True).encode()
            ).hexdigest(),
            "min_similarity": settings.memory.min_similarity,
            "context_max_chars": settings.memory.context_max_chars,
            "answer_model": settings.model.model_name,
            "embedding_model_version": runtime.memory.index.model_version,
            "source_sha256": source_hashes,
            "queries": rows,
            "metrics": {
                "candidate_recall_at_5": sum(
                    len(set(row["expected"]) & set(row["candidate_ranked"][:5])) / len(row["expected"])
                    for row in positive
                )
                / len(positive),
                "answer_support_precision": supported_correct / supported_count if supported_count else None,
                "positive_recall_at_5": sum(
                    len(set(row["expected"]) & set(row["ranked"][:5])) / len(row["expected"])
                    for row in positive
                )
                / len(positive),
                "positive_answer_accuracy": sum(row["answer_correct"] for row in positive) / len(positive),
                "correct_abstention_rate": sum(row["answer_correct"] for row in negative) / len(negative),
                "false_abstention_rate": sum(row["answer"]["status"] == "unknown" for row in positive)
                / len(positive),
                "unrelated_context_rate": sum(bool(set(row["ranked"]) - profile_keys) for row in negative)
                / len(negative),
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    if os.environ.get("SHOVEL_RUN_LIVE_TESTS") != "1":
        parser.error("Set SHOVEL_RUN_LIVE_TESTS=1 to authorize real-provider evaluation.")
    report = asyncio.run(
        evaluate(
            AppSettings.load(args.config), args.corpus.resolve(), create=args.create, cases_path=args.cases
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["metrics"]))


if __name__ == "__main__":
    main()
