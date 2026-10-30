from __future__ import annotations

import ast
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretBytes, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityScope,
    ConnectionRequirement,
    FunctionContract,
    FunctionImplementation,
    SideEffectProfile,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db import execution_repository
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.capability_repository import SQLiteCapabilityRepository
from shovel.db.database import create_async_session_factory
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.execution import EXECUTION_DDL
from shovel.db.model.security import ApprovalRequest as ApprovalRow
from shovel.db.operation_repository import SQLiteOperationReader
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.domain import ExecutionState, Invocation, InvocationContext
from shovel.execution.engine import ExecutionEngine
from shovel.execution.executors import AgentExecutor, FunctionExecutor, SkillExecutor, WorkflowExecutor
from shovel.execution.invocation import InvocationService
from shovel.runtime.client import list_operations, manage_operation
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.operation_reads import OperationQueries
from shovel.runtime.requests import request_operation_id
from shovel.runtime.service import Runtime
from shovel.security.domain import ApprovalState
from shovel.security.services import AuthorizationService, SecurityGate
from test_capability_registry import SCOPE, skill
from test_capability_registry import catalog as catalog
from test_execution_engine import Kernel, echo, invocation, publish, reference
from test_execution_engine import kernel as kernel
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_security_core import READ, SecureKernel, approve, capability, pending, request
from test_security_core import secured as secured


@pytest.mark.parametrize(
    "other_scope",
    [
        CapabilityScope(workspace_id=SCOPE.workspace_id, user_id="another-user"),
        CapabilityScope(workspace_id="another-workspace", user_id=SCOPE.user_id),
    ],
)
async def test_scoped_idempotency_and_execution_history_isolation(
    kernel: Kernel,
    other_scope: CapabilityScope,
) -> None:
    first = await kernel.invocations.invoke(invocation(await publish(kernel.catalog), request_key="same-key"))
    service = CapabilityService(kernel.catalog.registry, authorizer=AsyncMock())
    draft = await service.create_draft(
        other_scope,
        CapabilityKind.FUNCTION,
        "Other owner",
        CapabilityContent(implementation=FunctionImplementation(implementation_id="demo.read")),
    )
    version = await service.publish(other_scope, draft.capability_id, 1, expected_revision=1)
    other_request = Invocation(
        capability=reference(version),
        context=InvocationContext(scope=other_scope, actor_id=other_scope.user_id),
        request_key="same-key",
    )
    second = await kernel.invocations.invoke(other_request)
    assert first.id != second.id
    assert (await kernel.invocations.invoke(other_request)).id == second.id
    await kernel.engine.run(SCOPE, first.id)
    for read in (kernel.repository.get, kernel.repository.attempts, kernel.repository.steps):
        with pytest.raises(NotFound):
            await read(other_scope, first.id)
    with pytest.raises(NotFound):
        await kernel.engine.cancel(other_scope, first.id)
    with pytest.raises(NotFound):
        await kernel.catalog.registry.get_version(other_scope, first.snapshot.capability.capability_id, 1)
    assert (await kernel.repository.get(SCOPE, first.id)).state == ExecutionState.SUCCEEDED


async def test_step_is_an_immutable_dispatched_attempt_fact(kernel: Kernel) -> None:
    execution = await kernel.invocations.invoke(invocation(await publish(kernel.catalog)))
    lease = await kernel.engine.start(SCOPE, execution.id)
    assert await kernel.repository.steps(SCOPE, execution.id) == []
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    (step,) = await kernel.repository.steps(SCOPE, execution.id)
    assert step.state == ExecutionState.RUNNING and step.attempt_number == 1
    await kernel.engine.cancel(SCOPE, execution.id)
    (finished,) = await kernel.repository.steps(SCOPE, execution.id)
    assert finished.state == ExecutionState.CANCELLED and finished.finished_at == kernel.now[0]
    with pytest.raises(IntegrityError, match="immutable"):
        async with kernel.catalog.sessions() as session:
            await session.execute(
                text("UPDATE execution_step SET state='succeeded' WHERE execution_id=:id"),
                {"id": execution.id},
            )
            await session.commit()


async def test_payload_is_detached_before_waiting_for_the_write_lock(
    kernel: Kernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = await kernel.invocations.invoke(invocation(await publish(kernel.catalog)))
    payload = {"nested": {"safe": True}}
    write = execution_repository.sqlite_write

    @asynccontextmanager
    async def concurrent_write(sessions):
        payload["nested"]["password"] = "unpersisted-credential"
        async with write(sessions) as session:
            yield session

    monkeypatch.setattr(execution_repository, "sqlite_write", concurrent_write)
    persisted = await kernel.repository.create(original.snapshot, payload, "a" * 64)
    assert persisted.input == {"nested": {"safe": True}}
    assert (await kernel.repository.get(SCOPE, persisted.id)).input == persisted.input


async def test_approval_dispatch_budget_and_step_commit_atomically(secured: SecureKernel) -> None:
    execution = await pending(secured)
    assert await secured.repository.steps(SCOPE, execution.id) == []
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    lease = await secured.engine.start(SCOPE, execution.id)
    before = await secured.repository.get(SCOPE, execution.id)
    async with secured.catalog.sessions() as session:
        await session.execute(
            text(
                """CREATE TRIGGER injected_failure BEFORE INSERT ON execution_step
            BEGIN SELECT RAISE(ABORT, 'injected step failure'); END"""
            )
        )
        await session.commit()
    with pytest.raises(IntegrityError, match="injected step failure"):
        await secured.repository.dispatch(lease, secured.invocations.security)
    assert await secured.repository.get(SCOPE, execution.id) == before
    assert (await secured.repository.attempts(SCOPE, execution.id))[-1].dispatched_at is None
    assert await secured.repository.steps(SCOPE, execution.id) == []
    approval = await secured.approvals.get(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert approval.state == ApprovalState.APPROVED and approval.consumed_attempt is None
    async with secured.catalog.sessions() as session:
        await session.execute(text("DROP TRIGGER injected_failure"))
        await session.commit()
    await secured.repository.dispatch(lease, secured.invocations.security)
    completed = await secured.engine.succeed(lease, {})
    assert completed.usage.function_calls == 1
    (step,) = await secured.repository.steps(SCOPE, execution.id)
    assert step.attempt_number == 2 and step.state == ExecutionState.SUCCEEDED


@pytest.mark.parametrize("attempt", [2, 999])
async def test_approval_consumption_requires_a_unique_real_attempt(
    secured: SecureKernel,
    attempt: int,
) -> None:
    execution = await pending(secured)
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    await secured.engine.run(SCOPE, execution.id)
    with pytest.raises(IntegrityError, match=r"UNIQUE|execution attempt"):
        async with secured.catalog.sessions() as session:
            row = ApprovalRow(
                execution_id=execution.id,
                workspace_id=SCOPE.workspace_id,
                user_id=SCOPE.user_id,
                number=2,
                binding_hash="a" * 64,
                reasons=["test"],
                state="pending",
                created_at=1000,
                expires_at=1300,
            )
            session.add(row)
            await session.flush()
            row.state, row.decided_at = "approved", 1000
            await session.flush()
            row.state, row.consumed_at, row.consumed_attempt = "consumed", 1000, attempt
            await session.commit()


async def test_reopened_kernel_restores_frozen_lock_and_approved_execution(secured: SecureKernel) -> None:
    dependency = await capability(secured)
    version = await capability(secured, "external_write", children=(dependency,))
    original = await secured.invocations.invoke(request(version))
    suspended = await secured.engine.run(SCOPE, original.id)
    await approve(secured, suspended)
    engine = secured.catalog.sessions.kw["bind"]
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()
    sessions = create_async_session_factory(engine)
    repository = SQLiteExecutionRepository(sessions, clock=lambda: secured.now[0])
    registry = CapabilityRegistry(
        SQLiteCapabilityRepository(sessions),
        functions=tuple(
            FunctionContract(
                implementation_id=effect,
                required_permissions=(READ,),
                side_effect_profile=SideEffectProfile(kind=effect),
            )
            for effect in ("none", "external_write")
        ),
    )
    security = SecurityGate(AuthorizationService(secured.policies))
    invocations = InvocationService(registry, repository, security=security)
    restarted = ExecutionEngine(
        invocations,
        {
            CapabilityKind.FUNCTION: FunctionExecutor({"external_write": echo}),
            CapabilityKind.SKILL: SkillExecutor(),
            CapabilityKind.WORKFLOW: WorkflowExecutor(),
            CapabilityKind.AGENT: AgentExecutor(lambda: pytest.fail("No network client expected.")),
        },
        clock=lambda: secured.now[0],
    )
    try:
        restored = await repository.get(SCOPE, original.id)
        assert (
            restored.snapshot == original.snapshot
            and restored.snapshot.capability.content.dependency_lock.dependencies
        )
        approvals = SQLiteApprovalRepository(sessions, clock=lambda: secured.now[0])
        receipt = await approvals.get(
            SCOPE,
            original.id,
            suspended.approval_request_id,
            user_id=SCOPE.user_id,
        )
        assert receipt.state == ApprovalState.APPROVED
        await restarted.resume(SCOPE, original.id)
        completed = await restarted.run(SCOPE, original.id)
        assert completed.state == ExecutionState.SUCCEEDED and completed.snapshot == original.snapshot
        (step,) = await repository.steps(SCOPE, original.id)
        assert step.attempt_number == 2 and step.state == ExecutionState.SUCCEEDED
        assert (
            await approvals.get(
                SCOPE,
                original.id,
                suspended.approval_request_id,
                user_id=SCOPE.user_id,
            )
        ).consumed_attempt == 2
        with pytest.raises(NotFound):
            await approvals.get(
                CapabilityScope(workspace_id=SCOPE.workspace_id, user_id="another-user"),
                original.id,
                suspended.approval_request_id,
                user_id="another-user",
            )
    finally:
        await restarted.close()


async def test_version_nine_upgrade_preserves_request_ids_and_backfills_dispatch_facts(
    kernel: Kernel,
) -> None:
    original_request = invocation(await publish(kernel.catalog), request_key="old-receipt")
    execution = await kernel.invocations.invoke(original_request)
    legacy_id = request_operation_id("old-receipt")
    async with kernel.catalog.sessions() as session:
        await session.execute(text("DROP TRIGGER execution_snapshot_immutable"))
        await session.execute(
            text(
                "UPDATE operation SET id=:old, root_execution_id=:old, request_key='old-receipt' WHERE id=:id"
            ),
            {"id": execution.id, "old": legacy_id},
        )
        for statement in EXECUTION_DDL:
            await session.execute(text(statement))
        await session.commit()
    completed = await kernel.engine.run(SCOPE, legacy_id)
    async with kernel.catalog.sessions() as session:
        await session.execute(text("DELETE FROM schema_migration WHERE version=10"))
        await session.execute(text("DROP TABLE execution_step"))
        await session.commit()
    engine = kernel.catalog.sessions.kw["bind"]
    await upgrade_database(engine)
    await upgrade_database(engine)
    replayed = await kernel.invocations.invoke(original_request)
    assert replayed == completed and replayed.id == legacy_id
    (step,) = await kernel.repository.steps(SCOPE, legacy_id)
    assert step.state == ExecutionState.SUCCEEDED and step.started_at == kernel.now[0]


async def test_failed_migration_rolls_back_ddl_and_can_be_repaired(kernel: Kernel) -> None:
    execution = await kernel.invocations.invoke(invocation(await publish(kernel.catalog)))
    async with kernel.catalog.sessions() as session:
        await session.execute(text("DROP TRIGGER execution_snapshot_immutable"))
        await session.execute(
            text("UPDATE operation SET execution_data='{}' WHERE id=:id"), {"id": execution.id}
        )
        await session.execute(text("DELETE FROM schema_migration WHERE version=10"))
        await session.execute(text("DROP TABLE execution_step"))
        await session.commit()
    engine = kernel.catalog.sessions.kw["bind"]
    with pytest.raises(RuntimeFailure, match="ownership snapshots"):
        await upgrade_database(engine)
    async with kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM schema_migration WHERE version=10")) == 0
        assert (
            await session.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='execution_step'")) == 0
        )
        await session.execute(
            text("UPDATE operation SET execution_data=:data WHERE id=:id"),
            {"id": execution.id, "data": json.dumps(execution.snapshot.model_dump(mode="json"))},
        )
        await session.commit()
    await upgrade_database(engine)
    assert await kernel.repository.get(SCOPE, execution.id) == execution


async def test_write_unit_of_work_rolls_back_abort_and_cancellation(kernel: Kernel) -> None:
    async with kernel.catalog.sessions() as session:
        await session.execute(text("CREATE TABLE rollback_probe(value TEXT NOT NULL)"))
        await session.commit()
    with pytest.raises(ValueError, match="abort"):
        async with sqlite_write(kernel.catalog.sessions) as session:
            await session.execute(text("INSERT INTO rollback_probe VALUES ('aborted')"))
            raise ValueError("abort")
    entered = asyncio.Event()

    async def writer() -> None:
        async with sqlite_write(kernel.catalog.sessions) as session:
            await session.execute(text("INSERT INTO rollback_probe VALUES ('cancelled')"))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sqlite_write(kernel.catalog.sessions) as session:
        assert await session.scalar(text("SELECT count(*) FROM rollback_probe")) == 0
        await session.execute(text("INSERT INTO rollback_probe VALUES ('committed')"))
        await session.commit()
    async with kernel.catalog.sessions() as session:
        assert list(await session.scalars(text("SELECT value FROM rollback_probe"))) == ["committed"]


@pytest.mark.parametrize(
    "provenance",
    [
        {"api_key": "unpersisted-credential"},
        {"nested": {"Authorization": "unpersisted-credential"}},
        {"value": SecretStr("unpersisted-credential")},
        {"value": SecretBytes(b"unpersisted-credential")},
    ],
)
async def test_credential_fields_and_wrappers_are_rejected_before_persistence(
    kernel: Kernel,
    provenance: object,
) -> None:
    content = CapabilityContent(implementation=FunctionImplementation(implementation_id="demo.read"))
    unsafe = content.model_copy(update={"provenance": provenance})
    with pytest.raises(ValueError, match="Credentials"):
        await kernel.catalog.service.create_draft(SCOPE, CapabilityKind.FUNCTION, "Unsafe", unsafe)
    async with kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM capability_definition")) == 0
        assert await session.scalar(text("SELECT count(*) FROM capability_version")) == 0


async def test_json_round_trip_preserves_null_unicode_and_connection_reference(kernel: Kernel) -> None:
    draft = await kernel.catalog.service.create_draft(
        SCOPE,
        CapabilityKind.FUNCTION,
        "Connections",
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id="demo.read"),
            connection_requirements=(ConnectionRequirement(name="mail", connection_ref="conn-mail"),),
        ),
    )
    restored = await kernel.catalog.registry.get_version(SCOPE, draft.capability_id, 1)
    assert restored.content.connection_requirements[0].connection_ref == "conn-mail"
    payload = {"text": "\u4e2d\u6587", "nested": {"empty": None, "values": [1, True]}}
    execution = await kernel.invocations.invoke(invocation(await publish(kernel.catalog), input=payload))
    result = await kernel.engine.run(SCOPE, execution.id)
    assert result.input == payload and result.output == {"echo": payload}


async def test_legacy_api_and_offline_cli_hide_versioned_executions(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = runtime.platform.scope
    service = CapabilityService(runtime.capabilities, authorizer=AsyncMock())
    draft = await service.create_draft(scope, CapabilityKind.SKILL, "Legacy isolation", skill())
    version = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    request = invocation(version)
    execution = await runtime.invocations.invoke(
        request.model_copy(update={"context": request.context.model_copy(update={"scope": scope})})
    )
    legacy = await runtime.operations.enqueue("cleanup", {})
    assert (await api.get(f"/api/v1/operations/{execution.id}")).status_code == 404
    assert (await api.get(f"/api/v1/operations/{legacy.id}")).json()["id"] == legacy.id
    assert [row["id"] for row in (await api.get("/api/v1/operations")).json()["items"]] == [legacy.id]
    queries = OperationQueries(SQLiteOperationReader(runtime.sessions))
    with pytest.raises(NotFound):
        await queries.get(execution.id)
    assert [row["id"] for row in await list_operations(runtime.settings)] == [legacy.id]

    async def offline(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("shovel.runtime.client._remote", offline)
    with pytest.raises(NotFound):
        await manage_operation(runtime.settings, execution.id)


@pytest.mark.parametrize(
    "area", ["capability", "execution", "security", "automation", "workflow", "interaction"]
)
def test_kernel_domain_and_application_layers_do_not_import_persistence(area: str) -> None:
    source = Path(__file__).parent.parent / "src" / "shovel" / area
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [None])
        ]
        assert not any(name and name.startswith(("sqlalchemy", "shovel.db")) for name in imports), path
