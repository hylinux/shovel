from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import perf_counter, time
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shovel.automation.events import InProcessEventBus
from shovel.automation.manager import ProactiveServiceManager
from shovel.automation.schedules import ScheduleTriggerService
from shovel.capability.domain import CapabilityKind, CapabilityScope
from shovel.capability.registry import CapabilityRegistry
from shovel.config.settings import AppSettings
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.authentication_repository import SQLiteAuthenticationRepository
from shovel.db.capability_repository import SQLiteCapabilityRepository
from shovel.db.effect_repository import SQLiteEffectRepository
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.execution_control_repository import SQLiteExecutionControlRepository
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.experience_repository import SQLiteExperienceRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.interaction_repository import SQLiteInteractionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.operation_repository import SQLiteOperationReader
from shovel.db.platform_queries import SQLitePlatformQueries
from shovel.db.proactive_repository import SQLiteProactiveRepository
from shovel.db.workflow_repository import SQLiteWorkflowRepository
from shovel.enterprise.organization_service import OrganizationService
from shovel.enterprise.service import ConnectionService
from shovel.exceptions.pipeline import KnowledgeIndexUnavailableError, VectorSinkError
from shovel.execution.control import ExecutionWakeupService, RecoveryService
from shovel.execution.effects import ExternalEffectService
from shovel.execution.engine import ExecutionEngine
from shovel.execution.executors import AgentExecutor, FunctionExecutor, SkillExecutor, WorkflowExecutor
from shovel.execution.invocation import InvocationService
from shovel.identity.native_wam import NativeWamBroker, NativeWamResolver, installed_host
from shovel.identity.profiles import IdentityService
from shovel.identity.service import (
    AuthenticationService,
)
from shovel.identity.waits import AuthenticationRequests
from shovel.interaction.service import InteractionService, NotificationService
from shovel.memory.experience import ExperienceService
from shovel.pipeline.sink import IndexOpenTimings, ZvecChunkSink, open_chunk_sink
from shovel.platform.authorization import ApprovalAuthorization, ExplicitActions, InputAuthorization
from shovel.platform.policy import ConfiguredSecurityPolicy
from shovel.platform.service import PlatformApplication
from shovel.runtime.diagnostics import trace, trace_context
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.locking import RuntimeLock
from shovel.runtime.operation_reads import OperationQueries
from shovel.security.services import ApprovalService, AuthorizationService, SecurityGate
from shovel.services.resource_service import ResourceService
from shovel.workflow.service import WorkflowService

if TYPE_CHECKING:
    from shovel.runtime.agent_instructions import AgentInstructionsService
    from shovel.runtime.chat import ChatService
    from shovel.runtime.mcp_config import MCPConfigurationService
    from shovel.runtime.memory import MemoryService
    from shovel.runtime.memory_tasks import MemoryTaskService
    from shovel.runtime.operations import OperationService
    from shovel.runtime.performance import RuntimePerformance, RuntimeWarmup
    from shovel.runtime.retrieval import RetrievalService
    from shovel.runtime.retrieval_diagnostics import RetrievalDiagnostics


class Runtime:
    def __init__(
        self,
        settings: AppSettings,
        sessions: async_sessionmaker[AsyncSession],
        resources: ResourceService,
    ) -> None:
        from shovel.runtime.agent_instructions import AgentInstructionsService
        from shovel.runtime.chat import ChatService
        from shovel.runtime.mcp_config import MCPConfigurationService
        from shovel.runtime.memory import MemoryService
        from shovel.runtime.memory_tasks import MemoryTaskService
        from shovel.runtime.operations import OperationService
        from shovel.runtime.performance import RuntimePerformance, RuntimeWarmup
        from shovel.runtime.retrieval import RetrievalService
        from shovel.runtime.retrieval_diagnostics import RetrievalDiagnostics, diagnostic_environment
        from shovel.runtime.runtime_config import RuntimeConfigurationService
        from shovel.runtime.user_profile import UserProfileService

        self.settings = settings
        self.configuration = RuntimeConfigurationService(settings)
        if installed_host() and settings.agent.enable_python_extensions:
            raise RuntimeError("Trusted WAM Host requires Python extensions to remain disabled.")
        self.sessions = sessions
        self.resources = resources
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self.memory_wake = asyncio.Event()
        self.worker_state = "idle"
        self.worker_error: str | None = None
        self._sink: ZvecChunkSink | None = None
        self._sink_lock = asyncio.Lock()
        self._closing = False
        self._knowledge_close_task: asyncio.Task[None] | None = None
        self._maintenance_operation: str | None = None
        self._index_open: IndexOpenTimings | None = None
        self._index_open_started = 0.0
        self.initialization: dict[str, float] = {}
        self.performance: RuntimePerformance = RuntimePerformance(settings)
        self.capabilities = CapabilityRegistry(SQLiteCapabilityRepository(sessions))
        scope = CapabilityScope(
            workspace_id=hashlib.sha256(str(settings.agent.workspace.resolve()).encode()).hexdigest()[:24],
            user_id=settings.platform.user_id,
        )
        security = SecurityGate(
            AuthorizationService(
                ConfiguredSecurityPolicy(
                    scope,
                    lambda: self.settings.platform.security,
                )
            )
        )
        actions = ExplicitActions()
        self.invocations = InvocationService(
            self.capabilities, SQLiteExecutionRepository(sessions), security=security
        )
        self.approvals = ApprovalService(
            SQLiteApprovalRepository(sessions),
            self.invocations.security,
            authorizer=ApprovalAuthorization(actions),
        )
        self.events = InProcessEventBus()
        proactive_repository = SQLiteProactiveRepository(sessions)
        self.proactive = ProactiveServiceManager(proactive_repository, self.invocations, wake=self.wake.set)
        self.schedules = ScheduleTriggerService(proactive_repository, self.events)
        self.events.subscribe(self.proactive.handle_event)
        self.workflows = WorkflowService(
            SQLiteWorkflowRepository(sessions),
            self.invocations.security,
            bus=self.events,
            authorizer=InputAuthorization(actions),
        )
        control_repository = SQLiteExecutionControlRepository(sessions)
        self.wakeups = ExecutionWakeupService(control_repository)
        self.recovery = RecoveryService(control_repository, self.capabilities.retry_safe)
        interaction_repository = SQLiteInteractionRepository(sessions)
        self.interactions = InteractionService(
            interaction_repository, self.approvals, self.workflows, self.wakeups
        )
        self.notifications = NotificationService(interaction_repository)
        self.operation_queries = OperationQueries(SQLiteOperationReader(sessions))
        authentication_repository = SQLiteAuthenticationRepository(sessions)
        self.external_effects = ExternalEffectService(
            SQLiteEffectRepository(sessions), self.invocations.security, {}
        )
        self.execution_engine = ExecutionEngine(
            self.invocations,
            {
                CapabilityKind.FUNCTION: FunctionExecutor({}),
                CapabilityKind.SKILL: SkillExecutor(
                    lambda: self.chat.agent.client(),
                    model_scope=lambda: self.performance.models.source("platform"),
                    options=lambda: self.chat.agent.output_options(self.settings.model.max_output_tokens),
                ),
                CapabilityKind.WORKFLOW: WorkflowExecutor(),
                CapabilityKind.AGENT: AgentExecutor(
                    lambda: self.chat.agent.client(),
                    model_scope=lambda: self.performance.models.source("platform"),
                    options=lambda: self.chat.agent.output_options(self.settings.model.max_output_tokens),
                ),
            },
            workflow_context=self.workflows.bind,
            wakeups=self.wakeups,
            authentication_waits=authentication_repository,
            effects=self.external_effects,
        )
        self.native_wam = NativeWamBroker()
        authentication = AuthenticationService(
            NativeWamResolver(self.execution_engine, self.native_wam), self.native_wam
        )
        self.enterprise = OrganizationService(
            sessions, authentication, self.execution_engine, runtime_version=version("shovel")
        )
        self.invocations.admission = self.enterprise.admit
        security.connection_authorizer = self.enterprise.authorize_connection
        security.execution_authorizer = self.enterprise.revalidate_execution
        self.memory: MemoryService = MemoryService(
            settings,
            sessions,
            model_transport=lambda: self.performance.model_transport(
                settings.memory.llm if settings.memory.llm.is_configured else settings.model,
                "memory",
                self.stopping,
            ),
        )
        self.platform = PlatformApplication(
            scope,
            self.execution_engine,
            self.proactive,
            self.approvals,
            self.workflows,
            self.interactions,
            self.wakeups,
            SQLitePlatformQueries(sessions),
            actions,
            agent_permissions=lambda: self.settings.platform.agent_permissions,
            wake=self.wake.set,
            connections=ConnectionService(
                SQLiteConnectionRepository(sessions),
                self.enterprise.catalog,
            ),
            identities=IdentityService(SQLiteIdentityRepository(sessions)),
            authentication=authentication,
            authentication_requests=AuthenticationRequests(authentication_repository),
            experiences=ExperienceService(
                SQLiteExperienceRepository(sessions, scope, self.memory.owner), self.invocations.repository
            ),
        )
        self.operations: OperationService = OperationService(self)
        self.retrieval: RetrievalService = RetrievalService(self)
        self.retrieval_diagnostics: RetrievalDiagnostics = RetrievalDiagnostics(self)
        self.environment = diagnostic_environment()
        self.memory_tasks: MemoryTaskService = MemoryTaskService(self)
        self.user_profile = UserProfileService(sessions, self.memory.owner)
        self.instructions: AgentInstructionsService = AgentInstructionsService(
            settings.agent.workspace, settings.model.max_input_chars
        )
        from shovel.runtime.attachments import AttachmentService

        self.attachments = AttachmentService(self)
        self.mcp: MCPConfigurationService = MCPConfigurationService(settings.agent.workspace)
        from shovel.runtime.mcp_oauth import MCPOAuthService

        self.mcp_auth = MCPOAuthService(self.mcp)
        self.chat: ChatService = ChatService(self)
        self.warmup: RuntimeWarmup = RuntimeWarmup(self)

    def redact(self, message: str) -> str:
        keys = (
            self.settings.model.security_key,
            self.settings.embedding.api_key.get_secret_value(),
            self.settings.memory.llm.security_key,
            self.settings.memory.embedder.api_key.get_secret_value(),
        )
        for key in keys:
            if key:
                message = message.replace(key, "[redacted]")
        return message[:4000]

    def ready_sink(self) -> ZvecChunkSink | None:
        return self._sink

    def knowledge_index_status(self) -> dict[str, Any]:
        if self._sink is not None:
            snapshot = self._sink.admission_snapshot()
        else:
            snapshot = {
                "admission": "closing" if self._closing else "unopened",
                "maintenance": None,
                "ann_complete": None,
                "collection": None,
                "pending_io": 0,
                "active_io": False,
            }
        snapshot["operation_id"] = self._maintenance_operation
        if self._maintenance_operation is not None and not self._closing:
            snapshot["admission"] = "maintenance"
            snapshot["maintenance"] = snapshot["maintenance"] or (
                "opening" if self._sink is None else "checkpoint"
            )
        return snapshot

    def check_knowledge_index_available(self, *, maintenance_operation_id: str | None = None) -> None:
        if self._closing:
            raise RuntimeFailure("Knowledge index is closing.", status=503)
        if (
            self._maintenance_operation is not None
            and maintenance_operation_id != self._maintenance_operation
        ):
            raise KnowledgeIndexUnavailableError("maintenance")
        if self._sink is not None:
            self._sink.check_available()

    @contextmanager
    def knowledge_maintenance(self, operation_id: str) -> Iterator[None]:
        self.check_knowledge_index_available()
        self._maintenance_operation = operation_id
        try:
            yield
        finally:
            self._maintenance_operation = None

    def index_open_snapshot(self) -> dict[str, Any] | None:
        if self._index_open is None:
            return None
        snapshot = asdict(self._index_open)
        if self._index_open.state == "opening":
            snapshot["elapsed_ms"] = (perf_counter() - self._index_open_started) * 1000
        return snapshot

    def _index_activity(self) -> dict[str, object]:
        return {
            "foreground_runs": self.performance.running,
            "foreground_waiters": self.performance.foreground - self.performance.running,
            "legacy_worker_state": self.worker_state,
            "memory_operation_active": self.memory_tasks.is_active,
            "warmup_active": self.performance.warming,
        }

    async def sink(
        self,
        *,
        reason: Literal["warmup", "on_demand"] = "on_demand",
        maintenance_operation_id: str | None = None,
    ) -> ZvecChunkSink:
        self.check_knowledge_index_available(maintenance_operation_id=maintenance_operation_id)
        started = perf_counter()
        requested_at = time()
        async with self._sink_lock:
            if self._closing:
                raise RuntimeFailure("Knowledge index is closing.", status=503)
            if self._sink is None:
                measurement = IndexOpenTimings(
                    host_id=self.retrieval_diagnostics.host_id,
                    reason=reason,
                    attempt=self._index_open.attempt + 1 if self._index_open is not None else 1,
                    requested_at=requested_at,
                    lock_wait_ms=(perf_counter() - started) * 1000,
                    activity_before=self._index_activity(),
                )
                self._index_open, self._index_open_started = measurement, started
                opening = perf_counter()

                def open_sink() -> ZvecChunkSink:
                    measurement.phase = "worker"
                    measurement.worker_started_at = time()
                    measurement.queue_ms = (perf_counter() - opening) * 1000
                    trace("index.worker.started", queue_ms=round(measurement.queue_ms, 2))
                    return open_chunk_sink(self.settings.zvec.knowledge_path, measurement=measurement)

                with trace_context(
                    host_id=measurement.host_id,
                    index_open_id=measurement.operation_id,
                    reason=reason,
                ):
                    trace("index.opening", lock_wait_ms=round(measurement.lock_wait_ms, 2))
                    task = asyncio.create_task(asyncio.to_thread(open_sink))
                    try:
                        while not task.done():
                            try:
                                await asyncio.shield(task)
                            except asyncio.CancelledError:
                                measurement.caller_cancelled = True
                        self._sink = task.result()
                        if self._closing:
                            self._sink.stop_admission()
                        if measurement.caller_cancelled:
                            raise asyncio.CancelledError
                    except BaseException as exc:
                        if self._sink is None:
                            measurement.state = "failed"
                            measurement.error_type = measurement.error_type or type(exc).__name__
                            self.warmup.states["index"] = {
                                "state": "failed",
                                "error_type": measurement.error_type,
                            }
                        if isinstance(exc, FileNotFoundError):
                            raise VectorSinkError(
                                "知识库向量集合不存在,清理意图仍保留。",
                                hint=(
                                    "运行 shovel init 初始化向量集合;"
                                    "不配置模型也可以使用资源管理和关键词检索。"
                                ),
                            ) from exc
                        raise
                    finally:
                        if self._sink is not None:
                            measurement.phase, measurement.state = "complete", "ready"
                        measurement.completed_at = time()
                        measurement.elapsed_ms = (perf_counter() - started) * 1000
                        measurement.activity_after = self._index_activity()
                        trace("index.open.completed", **asdict(measurement))
                trace("index.opened", elapsed_ms=round((perf_counter() - opening) * 1000, 2))
                self.warmup.states["index"] = {
                    "state": "ready",
                    "elapsed_ms": round((perf_counter() - opening) * 1000, 2),
                }
            self.warmup.states["index"]["state"] = "ready"
            self.warmup.states["index"].pop("error_type", None)
            if self._closing:
                self._sink.stop_admission()
                raise RuntimeFailure("Knowledge index is closing.", status=503)
            return self._sink

    async def close(self) -> None:
        self._closing = True
        if self._sink is not None:
            self._sink.stop_admission()
        try:
            try:
                await self.events.stop()
            finally:
                try:
                    await self.native_wam.close()
                finally:
                    await self.execution_engine.close()
                    try:
                        await self.mcp_auth.close()
                    finally:
                        await self.chat.close()
        finally:
            try:
                await self.memory.close()
            finally:
                try:
                    await self.retrieval.close()
                finally:
                    if self._knowledge_close_task is None:
                        self._knowledge_close_task = asyncio.create_task(self._close_knowledge_index())
                    cancelled = False
                    while not self._knowledge_close_task.done():
                        try:
                            await asyncio.shield(self._knowledge_close_task)
                        except asyncio.CancelledError:
                            cancelled = True
                    self._knowledge_close_task.result()
                    if cancelled:
                        raise asyncio.CancelledError

    async def _close_knowledge_index(self) -> None:
        async with self._sink_lock:
            if self._sink is not None:
                await self._sink.aclose()
                self._sink = None


async def create_runtime(
    settings: AppSettings,
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    resources: ResourceService,
) -> AsyncGenerator[Runtime]:
    started, started_at = perf_counter(), time()
    with RuntimeLock(settings):
        trace("runtime.initializing")
        await asyncio.to_thread(_prepare_database_path, str(engine.url.database or ""))
        await upgrade_database(engine)
        runtime = Runtime(settings, sessions, resources)
        try:
            await runtime.memory.initialize()
            await runtime.user_profile.synchronize()
            await runtime.enterprise.refresh_policy()
            await runtime.recovery.recover()
            await runtime.interactions.refresh()
            await runtime.operations.recover()
            await runtime.chat.recover()
            await runtime.attachments.cleanup(force=True)
            runtime.initialization = {
                "started_at": started_at,
                "ready_at": time(),
                "elapsed_ms": (perf_counter() - started) * 1000,
            }
            trace(
                "runtime.ready",
                model_configured=settings.model.is_configured,
                embedding_configured=settings.embedding.is_configured,
                memory_configured=runtime.memory.configured,
            )
            yield runtime
        finally:
            trace("runtime.stopping")
            await runtime.close()
            trace("runtime.stopped")


def _prepare_database_path(database_url_path: str) -> None:
    if database_url_path and database_url_path != ":memory:":
        Path(database_url_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
