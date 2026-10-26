from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from starlette.types import ASGIApp, Receive, Scope, Send

from shovel.config.settings import AppSettings
from shovel.db.model.config import Resource, ScanProfile
from shovel.db.model.content import Chunk, Document
from shovel.db.model.execution import Job, JobEvent
from shovel.db.model.retrieval import DeletionOutbox
from shovel.db.model.runtime import ChatMessage, Conversation, Operation
from shovel.domain.enums import ResourceState
from shovel.exceptions.base import ShovelError
from shovel.exceptions.resource import (
    DuplicateResourceNameError,
    InvalidResourceStateTransitionError,
    ResourceNotFoundError,
    UnknownConnectorError,
)
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.hosting import resolve_runtime
from shovel.runtime.schemas import (
    CleanupInput,
    ConversationInput,
    CredentialInput,
    MessageInput,
    ReasonInput,
    ResourceInput,
    ResourceUpdate,
    ScanInput,
    SearchInput,
)
from shovel.runtime.service import Runtime
from shovel.services import connector_registry
from shovel.services.connector_base import ConnectorSpec


def resource_view(resource: Resource) -> dict[str, Any]:
    return {
        name: getattr(resource, name)
        for name in (
            "id",
            "name",
            "connector_kind",
            "state",
            "config",
            "description",
            "authority",
            "sensitivity",
            "state_reason",
            "identity_ref",
            "last_verified_at",
            "created_at",
            "updated_at",
        )
    }


def operation_view(operation: Operation) -> dict[str, Any]:
    return {
        name: getattr(operation, name)
        for name in (
            "id",
            "kind",
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


def job_view(job: Job, resource_name: str = "") -> dict[str, Any]:
    result = {
        name: getattr(job, name)
        for name in (
            "id",
            "resource_id",
            "scan_profile_id",
            "scan_mode",
            "state",
            "discovered",
            "skipped_unchanged",
            "fetched",
            "parsed",
            "chunked",
            "embedded",
            "failed_docs",
            "chunks_written",
            "bytes_read",
            "current_stage",
            "progress",
            "error_detail",
            "queued_at",
            "started_at",
            "finished_at",
            "heartbeat_at",
            "is_cancel_requested",
            "is_discovery_complete",
            "stats",
            "error_kind",
        )
    }
    return {**result, "resource_name": resource_name}


def message_view(message: ChatMessage) -> dict[str, Any]:
    return {
        name: getattr(message, name)
        for name in (
            "id",
            "conversation_id",
            "role",
            "content",
            "state",
            "citations",
            "warnings",
            "created_at",
            "sequence",
        )
    }


def connector_view(spec: ConnectorSpec) -> dict[str, Any]:
    return {
        "kind": spec.kind,
        "display_name": spec.display_name,
        "description": spec.description,
        "config_schema": spec.config_model.model_json_schema(),
        "capabilities": sorted(item.value for item in spec.capabilities),
        "supported_modes": sorted(item.value for item in spec.supported_modes()),
        "requires_identity": spec.requires_identity,
    }


async def current_runtime(request: Request) -> Runtime:
    provider = request.app.state.runtime_provider
    if not callable(provider):
        raise RuntimeError("Runtime provider is unavailable.")
    return await resolve_runtime(provider)


RuntimeDependency = Annotated[Runtime, Depends(current_runtime)]


class LocalAccessMiddleware:
    def __init__(self, app: ASGIApp, settings: AppSettings) -> None:
        self.app = app
        self.ports = {settings.http_port, settings.dev_http_port, settings.dev_frontend_port}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        hostname = request.url.hostname
        origin = request.headers.get("origin")
        invalid_origin = False
        if origin is not None:
            try:
                parsed_origin = urlsplit(origin)
                port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
                invalid_origin = (
                    parsed_origin.scheme not in {"http", "https"}
                    or parsed_origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or port not in self.ports
                )
            except ValueError:
                invalid_origin = True
        if (
            hostname not in {"127.0.0.1", "localhost", "::1"}
            or invalid_origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            await JSONResponse(
                {"error": {"code": "local_access_denied", "message": "仅允许本机 Shovel 界面访问。"}},
                status_code=403,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def configure_api(app: FastAPI, provider: Callable[[], object], settings: AppSettings) -> None:
    app.add_middleware(LocalAccessMiddleware, settings=settings)
    app.state.runtime_provider = provider
    router = APIRouter(prefix="/api/v1")

    @app.exception_handler(ShovelError)
    async def shovel_error(_request: Request, exc: ShovelError) -> JSONResponse:
        status = 400
        if isinstance(exc, RuntimeFailure):
            status = exc.status
        elif isinstance(exc, (ResourceNotFoundError, UnknownConnectorError)):
            status = 404
        elif isinstance(exc, (DuplicateResourceNameError, InvalidResourceStateTransitionError)):
            status = 409
        return JSONResponse(
            {"error": {"code": type(exc).__name__, "message": exc.message, "hint": exc.hint}},
            status_code=status,
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(_request: Request, exc: Any) -> JSONResponse:
        # Never echo request inputs: credential validation failures may contain secrets.
        details = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
        return JSONResponse(
            {"error": {"code": "validation_error", "message": "请求参数不合法。"}, "detail": details},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception) -> JSONResponse:
        structlog.get_logger("shovel.api").error("request.failed", error_type=type(exc).__name__)
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "服务内部错误,请检查本地日志。"}},
            status_code=500,
        )

    @router.get("/status")
    async def status(rt: RuntimeDependency) -> dict[str, Any]:
        from shovel.runtime.client import workspace_id

        return {
            "service": "shovel",
            "embedding_configured": rt.settings.embedding.is_configured,
            "model_configured": rt.settings.model.is_configured,
            "model_name": rt.settings.model.model_name,
            "memory_configured": rt.memory.configured,
            "workspace_id": workspace_id(rt.settings),
        }

    @router.get("/connectors")
    async def connectors() -> dict[str, Any]:
        return {"items": [connector_view(spec) for spec in connector_registry.all_specs()]}

    @router.get("/connectors/{kind}")
    async def connector(kind: str) -> dict[str, Any]:
        return connector_view(connector_registry.get_spec(kind))

    @router.get("/resources")
    async def resources(
        rt: RuntimeDependency,
        state: ResourceState | None = None,
        connector_kind: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        statement = select(Resource).order_by(Resource.name)
        if state is not None:
            statement = statement.where(Resource.state == state)
        if connector_kind is not None:
            statement = statement.where(Resource.connector_kind == connector_kind)
        async with rt.sessions() as session:
            rows = await session.scalars(statement.limit(limit).offset(offset))
            return {"items": [resource_view(row) for row in rows]}

    @router.get("/resources/{ref}")
    async def resource(ref: str, rt: RuntimeDependency) -> dict[str, Any]:
        row = await rt.resources.resolve(ref)
        async with rt.sessions() as session:
            profiles = await session.scalars(select(ScanProfile).where(ScanProfile.resource_id == row.id))
            counts = {
                "documents": await session.scalar(
                    select(func.count()).select_from(Document).where(Document.resource_id == row.id)
                ),
                "chunks": await session.scalar(
                    select(func.count()).select_from(Chunk).where(Chunk.resource_id == row.id)
                ),
            }
            return {
                **resource_view(row),
                **counts,
                "profiles": [
                    {"id": profile.id, "name": profile.name, "scan_mode": profile.scan_mode}
                    for profile in profiles
                ],
            }

    @router.post("/credentials", status_code=201)
    async def credential(body: CredentialInput) -> dict[str, str]:
        from shovel.runtime.client import save_credential

        reference = await save_credential(SecretStr(body.secret), body.identity_ref)
        return {"identity_ref": reference}

    @router.post("/resources", status_code=202)
    async def add_resource(body: ResourceInput, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.create_resource(body))

    @router.patch("/resources/{ref}", status_code=202)
    async def update_resource(ref: str, body: ResourceUpdate, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(ref, "update", body.model_dump(mode="json"))
        )

    @router.post("/resources/{ref}/verify", status_code=202)
    async def verify(ref: str, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "verify"))

    @router.post("/resources/{ref}/pause", status_code=202)
    async def pause(ref: str, rt: RuntimeDependency, body: ReasonInput | None = None) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(ref, "pause", (body or ReasonInput()).model_dump())
        )

    @router.post("/resources/{ref}/resume", status_code=202)
    async def resume(ref: str, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "resume"))

    @router.post("/resources/{ref}/revoke", status_code=202)
    async def revoke(ref: str, rt: RuntimeDependency, body: ReasonInput | None = None) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(ref, "revoke", (body or ReasonInput()).model_dump())
        )

    @router.delete("/resources/{ref}", status_code=202)
    async def remove(ref: str, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "delete"))

    @router.post("/resources/{ref}/scans", status_code=202)
    async def scan(ref: str, rt: RuntimeDependency, body: ScanInput | None = None) -> dict[str, Any]:
        return operation_view(await rt.operations.scan(ref, body or ScanInput()))

    @router.get("/operations")
    async def operations(
        rt: RuntimeDependency,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        async with rt.sessions() as session:
            rows = await session.scalars(
                select(Operation)
                .order_by(Operation.queued_at.desc(), Operation.id)
                .limit(limit)
                .offset(offset)
            )
            return {"items": [operation_view(row) for row in rows]}

    @router.get("/operations/{identifier}")
    async def operation(identifier: str, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.get(identifier))

    @router.post("/operations/{identifier}/retry", status_code=202)
    async def retry(identifier: str, rt: RuntimeDependency) -> dict[str, Any]:
        return operation_view(await rt.operations.retry(identifier))

    @router.get("/jobs")
    async def jobs(
        rt: RuntimeDependency,
        resource_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        statement = select(Job, Resource.name).join(Resource).order_by(Job.queued_at.desc(), Job.id)
        if resource_id is not None:
            statement = statement.where(Job.resource_id == (await rt.resources.resolve(resource_id)).id)
        async with rt.sessions() as session:
            rows = await session.execute(statement.limit(limit).offset(offset))
            return {"items": [job_view(job, name) for job, name in rows]}

    @router.get("/jobs/{identifier}")
    async def job(identifier: str, rt: RuntimeDependency) -> dict[str, Any]:
        async with rt.sessions() as session:
            row = await session.get(Job, identifier)
            if row is None:
                raise NotFound("扫描作业", identifier)
            source = await rt.resources.get(row.resource_id)
            return job_view(row, source.name)

    @router.get("/jobs/{identifier}/events")
    async def events(
        identifier: str,
        rt: RuntimeDependency,
        after_id: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        async with rt.sessions() as session:
            if await session.get(Job, identifier) is None:
                raise NotFound("扫描作业", identifier)
            rows = await session.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == identifier, JobEvent.id > after_id)
                .order_by(JobEvent.id)
                .limit(limit)
            )
            return {
                "items": [
                    {
                        name: getattr(row, name)
                        for name in (
                            "id",
                            "job_id",
                            "logged_at",
                            "level",
                            "stage",
                            "code",
                            "message",
                            "doc_uri",
                        )
                    }
                    for row in rows
                ]
            }

    @router.post("/jobs/{identifier}/cancel")
    async def cancel(identifier: str, rt: RuntimeDependency) -> dict[str, Any]:
        return job_view(await rt.operations.cancel_job(identifier))

    @router.post("/index/cleanup", status_code=202)
    async def cleanup(rt: RuntimeDependency, body: CleanupInput | None = None) -> dict[str, Any]:
        return operation_view(await rt.operations.enqueue("cleanup", (body or CleanupInput()).model_dump()))

    @router.get("/index/cleanup")
    async def cleanup_status(rt: RuntimeDependency) -> dict[str, Any]:
        async with rt.sessions() as session:
            rows = await session.scalars(
                select(DeletionOutbox)
                .where(DeletionOutbox.done_at.is_(None))
                .order_by(DeletionOutbox.id)
                .limit(100)
            )
            return {
                "items": [
                    {
                        name: getattr(row, name)
                        for name in ("id", "scope", "resource_id", "doc_uri", "attempts", "last_error")
                    }
                    for row in rows
                ]
            }

    @router.post("/search")
    async def search(body: SearchInput, rt: RuntimeDependency) -> dict[str, Any]:
        return await rt.retrieval.search(body)

    @router.get("/conversations")
    async def conversations(
        rt: RuntimeDependency,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        async with rt.sessions() as session:
            rows = await session.scalars(
                select(Conversation)
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(limit)
                .offset(offset)
            )
            return {
                "items": [
                    {name: getattr(row, name) for name in ("id", "title", "created_at", "updated_at")}
                    for row in rows
                ]
            }

    @router.post("/conversations", status_code=201)
    async def create_conversation(
        rt: RuntimeDependency, body: ConversationInput | None = None
    ) -> dict[str, Any]:
        async with rt.sessions() as session:
            row = Conversation(title=(body or ConversationInput()).title)
            session.add(row)
            await session.commit()
            return {name: getattr(row, name) for name in ("id", "title", "created_at", "updated_at")}

    @router.get("/conversations/{identifier}/messages")
    async def messages(identifier: str, rt: RuntimeDependency) -> dict[str, Any]:
        return {"items": [message_view(row) for row in await rt.chat.messages(identifier)]}

    @router.post("/conversations/{identifier}/messages")
    async def message(identifier: str, body: MessageInput, rt: RuntimeDependency) -> dict[str, Any]:
        return message_view(await rt.chat.send(identifier, body))

    @router.get("/memories")
    async def memories(rt: RuntimeDependency, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        return {"items": await rt.memory.list(limit)}

    @router.delete("/memories/{identifier}")
    async def forget(identifier: str, rt: RuntimeDependency) -> dict[str, bool]:
        await rt.memory.delete(identifier)
        return {"deleted": True}

    app.include_router(router)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def missing_api(path: str) -> None:
        raise NotFound("API", path)

    @app.api_route(
        "/api",
        methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def missing_api_root() -> None:
        raise NotFound("API", "/api")
