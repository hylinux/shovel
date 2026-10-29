from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Annotated, Any
from urllib.parse import urlsplit

import structlog
from ag_ui.core import BaseEvent, RunAgentInput
from ag_ui.encoder import EventEncoder
from anyio import CancelScope
from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
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
from shovel.runtime.agent_instructions import AgentInstructionsView
from shovel.runtime.diagnostics import DevelopmentRequestLogging, trace
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.hosting import resolve_runtime
from shovel.runtime.schemas import (
    AgentInstructionsInput,
    CleanupInput,
    CommandInput,
    ConversationInput,
    CredentialInput,
    HistoryPruneInput,
    MCPConfigurationInput,
    MemoryInput,
    MessageInput,
    ProfileInput,
    ReasonInput,
    ResourceInput,
    ResourceUpdate,
    ScanInput,
    SearchInput,
)
from shovel.runtime.service import Runtime
from shovel.services import connector_registry
from shovel.services.connector_base import ConnectorSpec
from shovel.services.outlook import OutlookError

_AGENT_HEARTBEAT_SECONDS = 10.0


async def _encode_agent_events(
    events: AsyncGenerator[BaseEvent], *, run_id: str | None = None
) -> AsyncIterator[str]:
    encoder = EventEncoder()
    pending: asyncio.Task[BaseEvent | None] | None = None
    try:
        while True:
            pending = asyncio.create_task(anext(events, None))
            while not pending.done():
                done, _ = await asyncio.wait((pending,), timeout=_AGENT_HEARTBEAT_SECONDS)
                if not done:
                    trace("agent.stream.heartbeat", run_id=run_id)
                    # Keep proxy sockets alive without cancelling the Agent or emitting protocol events.
                    yield ": keep-alive\n\n"
            completed, pending = pending, None
            event = completed.result()
            if event is None:
                return
            if event.type not in {
                "TEXT_MESSAGE_CONTENT",
                "TOOL_CALL_ARGS",
                "THINKING_TEXT_MESSAGE_CONTENT",
                "REASONING_MESSAGE_CONTENT",
            }:
                trace("agent.stream.event", run_id=run_id, event_type=event.type)
            yield encoder.encode(event)
    finally:
        # Starlette's disconnect cancel scope must not interrupt durable failure cleanup.
        with CancelScope(shield=True):
            try:
                if pending is not None:
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
            finally:
                await events.aclose()


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
            "memory_operation_id",
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
RequestKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


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


def configure_api(
    app: FastAPI,
    provider: Callable[[], object],
    settings: AppSettings,
    *,
    development: bool = False,
) -> None:
    app.add_middleware(LocalAccessMiddleware, settings=settings)
    if development:
        app.add_middleware(DevelopmentRequestLogging)
    app.state.runtime_provider = provider
    router = APIRouter(prefix="/api/v1")

    def mcp_response(rt: Runtime, result: dict[str, Any]) -> JSONResponse:
        loaded_revision = rt.chat.agent.extensions.mcp_revision
        result.update(
            agent_loaded=rt.chat.agent_state == "loaded",
            agent_state=rt.chat.agent_state,
            restart_required=rt.chat.agent_state == "restart_failed"
            or (loaded_revision is not None and loaded_revision != result["revision"]),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/agent/mcp")
    async def mcp_configuration(rt: RuntimeDependency) -> JSONResponse:
        return mcp_response(rt, await asyncio.to_thread(rt.mcp.read))

    @router.put("/agent/mcp")
    async def save_mcp_configuration(body: MCPConfigurationInput, rt: RuntimeDependency) -> JSONResponse:
        return mcp_response(rt, await asyncio.to_thread(rt.mcp.save, body.content, body.revision))

    @router.post("/agent/restart")
    async def restart_agent(rt: RuntimeDependency) -> JSONResponse:
        await rt.chat.restart_agent()
        return mcp_response(rt, await asyncio.to_thread(rt.mcp.read))

    def instructions_response(rt: Runtime, result: AgentInstructionsView) -> JSONResponse:
        loaded_revision = rt.chat.agent.instructions_revision
        state = rt.chat.agent_state
        return JSONResponse(
            {
                **result,
                "applied": loaded_revision == result["revision"]
                and state not in {"restarting", "restart_failed"},
                "agent_state": state,
                "restart_required": state == "restart_failed"
                or (loaded_revision is not None and loaded_revision != result["revision"]),
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/agent/instructions")
    async def agent_instructions(rt: RuntimeDependency) -> JSONResponse:
        return instructions_response(rt, await asyncio.to_thread(rt.instructions.read))

    @router.put("/agent/instructions")
    async def save_agent_instructions(body: AgentInstructionsInput, rt: RuntimeDependency) -> JSONResponse:
        return instructions_response(rt, await rt.chat.restart_agent(instructions=body))

    @app.exception_handler(ShovelError)
    async def shovel_error(_request: Request, exc: ShovelError) -> JSONResponse:
        status = 400
        if isinstance(exc, RuntimeFailure):
            status = exc.status
        elif isinstance(exc, (ResourceNotFoundError, UnknownConnectorError)):
            status = 404
        elif isinstance(exc, (DuplicateResourceNameError, InvalidResourceStateTransitionError)):
            status = 409
        elif isinstance(exc, OutlookError):
            status = 503
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

        if rt.worker_state == "failed":
            raise RuntimeFailure("后台 worker 已失败,Host 正在停止;持久任务将在重启后恢复。", status=503)
        return {
            "service": "shovel",
            "worker_state": rt.worker_state,
            "embedding_configured": rt.settings.embedding.is_configured,
            "model_configured": rt.settings.model.is_configured,
            "model_name": rt.settings.model.model_name,
            "memory_configured": rt.memory.configured,
            "memory": await rt.memory.status(),
            "workspace_id": workspace_id(rt.settings),
            "warmup": rt.warmup.states,
            "chat_capacity": rt.performance.snapshot(),
        }

    @router.get("/connectors")
    async def connectors() -> dict[str, Any]:
        return {"items": [connector_view(spec) for spec in connector_registry.all_specs()]}

    @router.get("/connectors/{kind}")
    async def connector(kind: str) -> dict[str, Any]:
        return connector_view(connector_registry.get_spec(kind))

    @router.get("/outlook/stores")
    async def outlook_stores() -> dict[str, Any]:
        from shovel.services.outlook import outlook_stores

        return {"items": [store.model_dump() for store in await outlook_stores()]}

    @router.get("/outlook/folders")
    async def outlook_folders(
        store_id: str = Query(min_length=1, max_length=4096),
        parent_id: str | None = Query(None, min_length=1, max_length=4096),
    ) -> dict[str, Any]:
        from shovel.services.outlook import outlook_folders

        return {"items": [folder.model_dump() for folder in await outlook_folders(store_id, parent_id)]}

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

    @router.post("/commands", status_code=202)
    async def command(
        body: CommandInput,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        from shovel.db.base import new_id
        from shovel.runtime.client import _submit_local

        identifier = await _submit_local(
            rt, body.kind, body.payload, body.ref, request_key or new_id("request")
        )
        return operation_view(await rt.operations.get(identifier))

    @router.get("/resources/{ref}/profiles")
    async def profiles(ref: str, rt: RuntimeDependency) -> dict[str, Any]:
        from shovel.runtime.profiles import profile_view

        resource = await rt.resources.resolve(ref)
        async with rt.sessions() as session:
            rows = await session.scalars(
                select(ScanProfile)
                .where(
                    ScanProfile.resource_id == resource.id,
                )
                .order_by(ScanProfile.name)
            )
            return {"items": [profile_view(row) for row in rows]}

    @router.put("/resources/{ref}/profiles/{name}", status_code=202)
    async def save_profile(
        ref: str,
        name: str,
        body: ProfileInput,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.profile_action(
                ref,
                name,
                body.model_dump(exclude_none=True),
                request_key=request_key,
            )
        )

    @router.delete("/resources/{ref}/profiles/{name}", status_code=202)
    async def delete_profile(
        ref: str,
        name: str,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        return operation_view(await rt.operations.profile_action(ref, name, None, request_key=request_key))

    @router.post("/history/prune", status_code=202)
    async def prune(
        body: HistoryPruneInput,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.enqueue(
                "history.prune",
                body.model_dump(),
                request_key=request_key,
            )
        )

    @router.post("/resources", status_code=202)
    async def add_resource(
        body: ResourceInput, rt: RuntimeDependency, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(await rt.operations.create_resource(body, request_key=request_key))

    @router.patch("/resources/{ref}", status_code=202)
    async def update_resource(
        ref: str, body: ResourceUpdate, rt: RuntimeDependency, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(
                ref, "update", body.model_dump(mode="json"), request_key=request_key
            )
        )

    @router.post("/resources/{ref}/verify", status_code=202)
    async def verify(ref: str, rt: RuntimeDependency, request_key: RequestKey = None) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "verify", request_key=request_key))

    @router.post("/resources/{ref}/pause", status_code=202)
    async def pause(
        ref: str, rt: RuntimeDependency, body: ReasonInput | None = None, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(
                ref, "pause", (body or ReasonInput()).model_dump(), request_key=request_key
            )
        )

    @router.post("/resources/{ref}/resume", status_code=202)
    async def resume(ref: str, rt: RuntimeDependency, request_key: RequestKey = None) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "resume", request_key=request_key))

    @router.post("/resources/{ref}/revoke", status_code=202)
    async def revoke(
        ref: str, rt: RuntimeDependency, body: ReasonInput | None = None, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.resource_action(
                ref, "revoke", (body or ReasonInput()).model_dump(), request_key=request_key
            )
        )

    @router.delete("/resources/{ref}", status_code=202)
    async def remove(ref: str, rt: RuntimeDependency, request_key: RequestKey = None) -> dict[str, Any]:
        return operation_view(await rt.operations.resource_action(ref, "delete", request_key=request_key))

    @router.post("/resources/{ref}/scans", status_code=202)
    async def scan(
        ref: str, rt: RuntimeDependency, body: ScanInput | None = None, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(await rt.operations.scan(ref, body or ScanInput(), request_key=request_key))

    @router.get("/operations")
    async def operations(
        rt: RuntimeDependency,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        state: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, Any]:
        statement = select(Operation).order_by(Operation.queued_at.desc(), Operation.id)
        if state is not None:
            statement = statement.where(Operation.state == state)
        if resource_id is not None:
            statement = statement.where(Operation.resource_id == resource_id)
        async with rt.sessions() as session:
            rows = await session.scalars(statement.limit(limit).offset(offset))
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
    async def cleanup(
        rt: RuntimeDependency, body: CleanupInput | None = None, request_key: RequestKey = None
    ) -> dict[str, Any]:
        return operation_view(
            await rt.operations.enqueue(
                "cleanup", (body or CleanupInput()).model_dump(), request_key=request_key
            )
        )

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
    async def messages(
        identifier: str,
        rt: RuntimeDependency,
        limit: int = Query(100, ge=1, le=1000),
        before: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        rows = await rt.chat.messages(identifier, limit=limit, before=before)
        return {
            "items": [message_view(row) for row in rows],
            "next_before": rows[0].sequence if len(rows) == limit else None,
        }

    @router.patch("/conversations/{identifier}")
    async def rename_conversation(
        identifier: str,
        body: ConversationInput,
        rt: RuntimeDependency,
    ) -> dict[str, Any]:
        await rt.chat.maintain(identifier, title=body.title)
        return {"id": identifier, "title": body.title}

    @router.delete("/conversations/{identifier}")
    async def delete_conversation(identifier: str, rt: RuntimeDependency) -> dict[str, bool]:
        await rt.chat.maintain(identifier)
        return {"deleted": True}

    @router.post("/conversations/{identifier}/messages")
    async def message(identifier: str, body: MessageInput, rt: RuntimeDependency) -> dict[str, Any]:
        return message_view(await rt.chat.send(identifier, body))

    @router.post("/agent")
    async def agent_run(body: RunAgentInput, rt: RuntimeDependency) -> StreamingResponse:
        await rt.chat.conversation(body.thread_id)
        if not body.messages or body.messages[-1].role != "user":
            raise RuntimeFailure("AG-UI 请求必须以用户文本消息结束。")
        # The database, not client-supplied system messages or history, owns the conversation.
        content = body.messages[-1].content
        if not isinstance(content, str):
            raise RuntimeFailure("当前 Agent 仅接受文本消息。")
        properties = body.forwarded_props or {}
        if not isinstance(properties, dict):
            raise RuntimeFailure("AG-UI forwardedProps 必须是对象。")
        request = MessageInput.model_validate({**properties, "content": content})
        trace(
            "agent.request.received",
            run_id=body.run_id,
            conversation_id=body.thread_id,
            input_chars=len(content),
            client_messages=len(body.messages),
            resource_count=len(request.resource_ids),
            memory_enabled=request.memory_enabled,
            search_mode=request.search_mode,
        )

        return StreamingResponse(
            _encode_agent_events(rt.chat.stream(body.thread_id, request, body.run_id), run_id=body.run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @router.get("/memories")
    async def memories(
        rt: RuntimeDependency,
        limit: int = Query(100, ge=1, le=1000),
        include_history: bool = False,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        if conversation_id:
            await rt.chat.conversation(conversation_id)
        return {
            "items": await rt.memory.list(
                limit, include_history=include_history, conversation_id=conversation_id
            )
        }

    @router.post("/memories", status_code=202)
    async def create_memory(
        body: MemoryInput,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        async with rt.sessions() as session:
            generation = await rt.memory_tasks.generation(session)
        operation = await rt.operations.enqueue(
            "memory.create",
            {
                "memory": body.model_dump(mode="json"),
                "workspace_id": rt.memory.owner.workspace_id,
                "user_id": rt.memory.owner.user_id,
                "generation": generation,
            },
            request_key=request_key,
        )
        rt.memory_wake.set()
        return operation_view(operation)

    @router.put("/memories/{identifier}", status_code=202)
    async def update_memory(
        identifier: str,
        body: MemoryInput,
        rt: RuntimeDependency,
        request_key: RequestKey = None,
    ) -> dict[str, Any]:
        current = await rt.memory.repository.get(
            replace(rt.memory.owner, session_id=body.session_id), identifier
        )
        if current.kind.value != body.kind:
            raise RuntimeFailure("更新不能更改 Memory 类型。")
        async with rt.sessions() as session:
            generation = await rt.memory_tasks.generation(session)
        operation = await rt.operations.enqueue(
            "memory.update",
            {
                "memory": body.model_dump(mode="json"),
                "workspace_id": rt.memory.owner.workspace_id,
                "user_id": rt.memory.owner.user_id,
                "generation": generation,
                "replace_id": identifier,
            },
            request_key=request_key,
        )
        rt.memory_wake.set()
        return operation_view(operation)

    @router.post("/memory/index/rebuild", status_code=202)
    async def rebuild_memory(rt: RuntimeDependency, request_key: RequestKey = None) -> dict[str, Any]:
        async with rt.sessions() as session:
            generation = await rt.memory_tasks.generation(session)
        operation = await rt.operations.enqueue(
            "memory.rebuild",
            {
                "workspace_id": rt.memory.owner.workspace_id,
                "user_id": rt.memory.owner.user_id,
                "generation": generation,
            },
            request_key=request_key,
        )
        rt.memory_wake.set()
        return operation_view(operation)

    @router.delete("/memories/{identifier}")
    async def forget(
        identifier: str,
        rt: RuntimeDependency,
        hard: bool = False,
        conversation_id: str = "",
    ) -> dict[str, bool]:
        current = await rt.memory.owned(identifier, conversation_id=conversation_id)
        if current.state.value == "deleted" and not hard:
            return {"deleted": True}
        await rt.memory_tasks.invalidate()
        await rt.memory.delete(identifier, hard=hard, conversation_id=conversation_id)
        rt.memory_wake.set()
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
