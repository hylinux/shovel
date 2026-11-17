from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from shovel.exceptions.pipeline import KnowledgeIndexUnavailableError
from shovel.pipeline.sink import VectorRecord, VectorSearchTimings, ZvecChunkSink
from shovel.runtime.knowledge_worker import KnowledgeWorker


class _RemoteCollection:
    def __init__(self, worker: KnowledgeWorker) -> None:
        self.worker = worker
        self.query_timings: dict[str, Any] = {}

    def upsert(self, records: list[VectorRecord]) -> None:
        self.worker.call("upsert", {"records": [asdict(record) for record in records]})

    def delete_by_filter(self, expression: str) -> None:
        self.worker.call("delete", {"filter": expression})

    def flush(self) -> None:
        # Mutating replies acknowledge the child's native flush, never just IPC receipt.
        return None

    def query(
        self, *, queries: Any, topk: int, filter: str, output_fields: list[str]
    ) -> list[SimpleNamespace]:
        result = self.worker.call("search", {"vector": queries.vector, "limit": topk, "filter": filter})
        self.query_timings = result["timings"]
        return [SimpleNamespace(id=identifier, score=score) for identifier, score in result["rows"]]

    def optimize(self, option: Any) -> None:
        self.worker.call("optimize", {"concurrency": option.concurrency})

    def close(self) -> None:
        self.worker.close()


class ProcessChunkSink(ZvecChunkSink):
    def __init__(self, worker: KnowledgeWorker) -> None:
        super().__init__(_RemoteCollection(worker))
        self.worker = worker

    async def _io[T](
        self,
        action: Callable[[], T],
        *,
        operation: str = "other",
        measurement: VectorSearchTimings | None = None,
    ) -> T:
        native: dict[str, Any] = {}

        def invoke() -> T:
            result = action()
            if measurement is not None:
                native.update(self._collection.query_timings)
            return result

        try:
            return await super()._io(invoke, operation=operation, measurement=measurement)
        finally:
            if measurement is not None and native:
                measurement.ipc_ms = max(0, measurement.native_ms - native["total_ms"])
                measurement.worker_queue_ms = native["queue_ms"]
                measurement.worker_lock_wait_ms = native["lock_wait_ms"]
                measurement.native_ms = measurement.query_ms = native["native_ms"]
                measurement.host_process_cpu_ms = measurement.process_cpu_ms
                measurement.process_cpu_ms = native["process_cpu_ms"]

    def check_available(self) -> None:
        super().check_available()
        if self.worker.interrupted or not self.worker.snapshot()["alive"]:
            raise KnowledgeIndexUnavailableError("closed")

    async def aclose(self) -> None:
        if self.worker._stop_task is None:
            self.worker.begin_shutdown(asyncio.get_running_loop().time() + 60)
        await super().aclose()

    @staticmethod
    def _to_doc(record: VectorRecord) -> VectorRecord:
        return record

    def _snapshot(self) -> dict[str, Any]:
        value: dict[str, Any] = self.worker.call("metadata", {})
        return value

    def admission_snapshot(self) -> dict[str, Any]:
        value = super().admission_snapshot()
        value["worker"] = self.worker.snapshot()
        if not value["worker"]["alive"]:
            value["admission"] = "closed"
        return value
