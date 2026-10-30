from __future__ import annotations

from typing import Any, Protocol


class OperationRecord(Protocol):
    id: str
    kind: str
    request_key: str | None
    state: str
    resource_id: str | None
    job_id: str | None
    result: dict[str, Any]
    error: str | None
    queued_at: int
    started_at: int | None
    finished_at: int | None
    attempts: int


def operation_view(operation: OperationRecord) -> dict[str, Any]:
    return {
        name: getattr(operation, name)
        for name in (
            "id",
            "kind",
            "request_key",
            "state",
            "resource_id",
            "job_id",
            "result",
            "error",
            "queued_at",
            "started_at",
            "finished_at",
            "attempts",
        )
    }


class OperationReadRepository(Protocol):
    async def get(self, identifier: str) -> dict[str, Any]: ...
    async def list(
        self,
        *,
        limit: int,
        offset: int,
        state: str | None,
        resource_id: str | None,
    ) -> list[dict[str, Any]]: ...


class OperationQueries:
    def __init__(self, repository: OperationReadRepository) -> None:
        self.repository = repository

    async def get(self, identifier: str) -> dict[str, Any]:
        return await self.repository.get(identifier)

    async def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        state: str | None = None,
        resource_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.repository.list(limit=limit, offset=offset, state=state, resource_id=resource_id)
