from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import struct
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from statistics import median
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import func, select

from shovel.db.base import now_ts
from shovel.db.model.runtime import Operation
from shovel.pipeline.sink import VectorSearchTimings
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.retrieval import RetrievalMeasurement
from shovel.runtime.schemas import RetrievalDiagnosticInput, SearchInput

if TYPE_CHECKING:
    from shovel.runtime.service import Runtime


def diagnostic_environment() -> dict[str, Any]:
    try:
        zvec_version = version("zvec")
    except PackageNotFoundError:
        zvec_version = "not_installed"
    return {
        "python": platform.python_version(),
        "zvec": zvec_version,
        "os": platform.system(),
        "machine": platform.machine(),
        "process_bits": struct.calcsize("P") * 8,
        "physical_disk_io": "not_collected",
        "hard_page_faults": "not_collected",
        "storage_medium": "not_collected",
        "architecture_emulation": "not_determined",
    }


class RetrievalDiagnostics:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.host_id = uuid4().hex
        self._lock = asyncio.Lock()

    async def _activity(self) -> dict[str, Any]:
        async with self.runtime.sessions() as session:
            running = await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(
                    Operation.state == "running",
                    Operation.capability_version_id.is_(None),
                    Operation.execution_data.is_(None),
                )
            )
        return {
            "running_legacy_operations": running,
            "foreground_runs": self.runtime.performance.running,
            "warmup_active": self.runtime.performance.warming,
        }

    async def run(
        self, request: RetrievalDiagnosticInput, *, disconnected: Callable[[], Awaitable[bool]]
    ) -> dict[str, Any]:
        if self._lock.locked():
            raise RuntimeFailure("A retrieval diagnostic is already running in this Host.", status=409)
        async with self._lock:
            return await self._run(request, disconnected)

    async def _run(
        self, request: RetrievalDiagnosticInput, disconnected: Callable[[], Awaitable[bool]]
    ) -> dict[str, Any]:
        runtime = self.runtime
        if not runtime.settings.embedding.is_configured:
            raise RuntimeFailure("Vector diagnostics require configured knowledge embeddings.", status=503)
        scope = await runtime.retrieval._scope(request.resource_ids)
        if not scope:
            raise RuntimeFailure("No readable resources are available for vector diagnostics.", status=409)
        started = perf_counter()
        started_at = now_ts()
        samples: list[dict[str, Any]] = []
        stop_reason: str | None = None

        async def can_continue() -> bool:
            nonlocal stop_reason
            if runtime.stopping.is_set():
                stop_reason = "host_stopping"
            elif await disconnected():
                stop_reason = "client_disconnected"
            elif perf_counter() - started >= request.budget_seconds:
                stop_reason = "budget_exhausted"
            else:
                await runtime.retrieval._scope(scope)
                return True
            return False

        for case, query in enumerate([request.query, *request.comparison_queries]):
            if not await can_continue():
                break
            activity = await self._activity()
            if not await can_continue():
                break
            measured = RetrievalMeasurement(scope=scope)
            found = await runtime.retrieval.search(
                SearchInput(
                    query=query, resource_ids=request.resource_ids, mode="vector", limit=request.limit
                ),
                measurement=measured,
            )
            samples.append(
                {
                    "query_case": case,
                    "kind": "full_retrieval",
                    "iteration": 0,
                    "phases_ms": measured.phases_ms,
                    "native": asdict(measured.native),
                    "final_hit_count": len(found["items"]),
                    "activity": activity,
                }
            )
            vector, model_version = measured.vector, measured.model_version
            sink = runtime.ready_sink()
            if sink is None or vector is None or model_version is None:
                raise RuntimeFailure(
                    "Vector diagnostic did not obtain an initialized sink and vector.", status=503
                )
            for iteration in range(1, request.repetitions + 1):
                if not await can_continue():
                    break
                if runtime.ready_sink() is not sink:
                    raise RuntimeFailure("Knowledge index changed during diagnostics.", status=409)
                activity = await self._activity()
                if not await can_continue():
                    break
                native = VectorSearchTimings()
                matches = await sink.search(
                    vector,
                    resource_ids=scope,
                    model_version=model_version,
                    limit=min(300, request.limit * 10),
                    measurement=native,
                )
                validating = perf_counter()
                valid = await runtime.retrieval.revalidate_vector_ids(
                    scope, [identifier for identifier, _score in matches], model_version
                )
                await runtime.retrieval._scope(scope)
                samples.append(
                    {
                        "query_case": case,
                        "kind": "native_repeat",
                        "iteration": iteration,
                        "native": asdict(native),
                        "validated_candidate_count": len(valid),
                        "revalidation_ms": (perf_counter() - validating) * 1000,
                        "activity": activity,
                    }
                )
            if stop_reason is not None:
                break

        # Metadata inspection follows queries so it cannot warm the first observed native search.
        metadata: dict[str, Any] | None = None
        counts: dict[str, Any] | None = None
        metadata_ms: float | None = None
        if runtime.stopping.is_set():
            stop_reason = "host_stopping"
        elif await disconnected():
            stop_reason = "client_disconnected"
        elif samples:
            await runtime.retrieval._scope(scope)
            sink = runtime.ready_sink()
            if sink is None:
                raise RuntimeFailure("Knowledge index is no longer available.", status=409)
            inspecting = perf_counter()
            metadata = await sink.diagnostic_snapshot()
            assert measured.model_version is not None
            counts = await runtime.retrieval.diagnostic_counts(scope, measured.model_version)
            metadata_ms = (perf_counter() - inspecting) * 1000
        summaries = []
        for case in range(1 + len(request.comparison_queries)):
            case_samples = [sample for sample in samples if sample["query_case"] == case]
            repeats = [sample["native"]["query_ms"] for sample in case_samples[1:]]
            if case_samples:
                summaries.append(
                    {
                        "query_case": case,
                        "first_observed_query_ms": case_samples[0]["native"]["query_ms"],
                        "repeat_query_median_ms": median(repeats) if repeats else None,
                        "repeat_query_max_ms": max(repeats) if repeats else None,
                        "repeat_count": len(repeats),
                    }
                )
        return {
            "report_version": 1,
            "host_id": self.host_id,
            "runtime_initialization": runtime.initialization,
            "index_open": runtime.index_open_snapshot(),
            "diagnostic_id": uuid4().hex,
            "workspace_id": request.workspace_id,
            "configuration_id": request.configuration_id,
            "generated_at": now_ts(),
            "started_at": started_at,
            "status": "partial" if stop_reason else "complete",
            "stop_reason": stop_reason,
            "elapsed_ms": (perf_counter() - started) * 1000,
            "resource_count": len(scope),
            "scope_fingerprint": hashlib.sha256(json.dumps(scope).encode()).hexdigest(),
            "budget_seconds": request.budget_seconds,
            "requested_limit": request.limit,
            "native_topk": min(300, request.limit * 10),
            "requested_repetitions": request.repetitions,
            "query_case_count": 1 + len(request.comparison_queries),
            "samples": samples,
            "summaries": summaries,
            "collection": metadata,
            "sqlite": counts,
            "metadata_ms": metadata_ms,
            "environment": runtime.environment,
            "limitations": [
                "Index opening is the latest actual open in this Host, not this request's index wait.",
                "Runtime initialization excludes Web listener binding; active open snapshots are partial.",
                "First observed query is not proof of cold OS or disk caches.",
                "Process CPU includes other threads; activity snapshots can miss brief concurrent work.",
                "The budget stops subsequent samples, not in-flight native I/O or final metadata inspection.",
                "Collection totals include stale/other resources; SQLite counts are scope/model specific.",
                "Full searches persist RetrievalLogs; reports omit queries, vectors and source data.",
            ],
        }
