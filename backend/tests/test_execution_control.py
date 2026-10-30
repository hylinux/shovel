from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from io import StringIO
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import select, text

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    DependencyLock,
    RiskProfile,
    SideEffectProfile,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.capability.service import CapabilityService
from shovel.db.execution_control_repository import SQLiteExecutionControlRepository
from shovel.db.interaction_repository import SQLiteInteractionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.control import ExecutionWakeup
from shovel.db.model.interaction import InputRequest, Interaction, NotificationDelivery
from shovel.db.workflow_repository import SQLiteWorkflowRepository
from shovel.execution.control import (
    ExecutionWakeupService,
    RecoveryService,
    RetryPolicy,
    TransientExecutionError,
)
from shovel.execution.domain import (
    ChildInvocation,
    ExecutionBudget,
    ExecutionState,
    ExecutorResult,
    Invocation,
    InvocationContext,
)
from shovel.execution.executors import FunctionExecutor
from shovel.execution.ports import ExecutorContext
from shovel.interaction.domain import InteractionAction
from shovel.interaction.service import InteractionService, NotificationService, RichNotificationSink
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.hosting import ExecutionMaintenanceWorker
from shovel.runtime.service import Runtime
from shovel.workflow.domain import WorkflowCheckpoint
from shovel.workflow.service import WorkflowService
from test_capability_registry import SCOPE, locked
from test_capability_registry import catalog as catalog
from test_execution_engine import reference
from test_hosted_runtime import runtime as runtime
from test_security_core import ALL, READ, SecureKernel, approve, capability, pending
from test_security_core import secured as secured
from test_workflow import Flow, answer, graph, waiting
from test_workflow import flow as flow


@dataclass
class Control:
    flow: Flow
    wakeups: ExecutionWakeupService
    recovery: RecoveryService
    interactions: InteractionService
    repository: SQLiteInteractionRepository

    async def tick(self) -> None:
        await self.wakeups.run_due()
        await self.interactions.refresh()

    async def queued(self):
        version = await self.flow.function()
        return await self.flow.invocations.invoke(
            Invocation(
                capability=reference(version),
                context=InvocationContext(scope=SCOPE, actor_id="owner", permission_ceiling=ALL),
                input={"value": 7},
            )
        )


@pytest.fixture
def control(flow: Flow) -> Control:
    repository = SQLiteExecutionControlRepository(flow.catalog.sessions, clock=lambda: flow.now[0])
    wakeups = ExecutionWakeupService(repository)
    flow.engine.wakeups = wakeups
    interactions = SQLiteInteractionRepository(flow.catalog.sessions, clock=lambda: flow.now[0])
    return Control(
        flow,
        wakeups,
        RecoveryService(repository, flow.catalog.registry.retry_safe),
        InteractionService(interactions, flow.approvals, flow.controls, wakeups),
        interactions,
    )


async def test_snooze_persists_execution_timer_not_business_schedule(control: Control) -> None:
    flow = control.flow
    execution = await control.queued()
    timer = await control.wakeups.snooze(SCOPE, execution.id, wake_at=1005)
    assert timer.state == "pending"
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.SUSPENDED
    async with flow.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM proactive_service")) == 0
        assert await session.scalar(text("SELECT count(*) FROM schedule")) == 0
    with pytest.raises(RuntimeFailure, match="not due"):
        await flow.engine.resume(SCOPE, execution.id)
    flow.now[0] = 1005
    await control.tick()
    await flow.engine.run(SCOPE, execution.id)
    assert len(flow.calls) == 1
    await control.tick()
    assert not await flow.engine.run_once()


async def test_delay_result_and_duplicate_timer(control: Control) -> None:
    flow = control.flow
    calls = 0

    async def delay(context: ExecutorContext) -> ExecutorResult:
        nonlocal calls
        calls += 1
        return (
            ExecutorResult(suspended=True, wake_at=1003)
            if calls == 1
            else ExecutorResult(output=context.execution.input)
        )

    flow.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"echo": delay})
    execution = await control.queued()
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUSPENDED and result.wait_reason == "delay"
    old = await control.wakeups.delay(SCOPE, execution.id, wake_at=1003)
    same = await control.wakeups.delay(SCOPE, execution.id, wake_at=1003)
    assert old.id == same.id
    flow.now[0] = 1003
    await control.tick()
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED
    assert calls == 2


@pytest.mark.parametrize("permanent,always_fail", [(False, False), (False, True), (True, True)])
async def test_retry_backoff_max_attempts_and_permanent_failure(
    control: Control, permanent: bool, always_fail: bool
) -> None:
    flow, calls = control.flow, 0

    async def attempt(context: ExecutorContext) -> ExecutorResult:
        nonlocal calls
        calls += 1
        if permanent:
            raise RuntimeFailure("Permanent.")
        if always_fail or calls < 3:
            raise TransientExecutionError("Temporary.")
        return ExecutorResult(output=context.execution.input)

    flow.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"echo": attempt})
    execution = await control.queued()
    result = await flow.engine.run(SCOPE, execution.id)
    if permanent:
        assert result.state == ExecutionState.FAILED and calls == 1
        async with flow.catalog.sessions() as session:
            assert not (await session.scalars(select(ExecutionWakeup))).all()
        return
    for when in (1001, 1003):
        assert result.state == ExecutionState.SUSPENDED
        assert result.wait_reason == "retry_scheduled" and result.control["wake_at"] == when
        flow.now[0] = when
        await control.tick()
        result = await flow.engine.run(SCOPE, execution.id)
    assert result.state == (ExecutionState.FAILED if always_fail else ExecutionState.SUCCEEDED)
    assert calls == 3 and result.attempts == 3
    history = await flow.repository.attempts(SCOPE, result.id)
    assert [attempt.reason for attempt in history[:2]] == ["transient_failure", "transient_failure"]
    await control.tick()
    assert not await flow.engine.run_once()


async def test_crash_recovery_fences_stale_claim_and_reuses_version(control: Control) -> None:
    flow = control.flow
    execution = await control.queued()
    lease = await flow.engine.start(SCOPE, execution.id)
    await flow.repository.dispatch(lease, flow.invocations.security)
    assert await control.recovery.recover() == 1
    interrupted = await flow.repository.get(SCOPE, execution.id)
    assert interrupted.state == ExecutionState.SUSPENDED and interrupted.wait_reason == "interrupted"
    assert (await flow.repository.attempts(SCOPE, execution.id))[0].reason == "interrupted"
    with pytest.raises(RuntimeFailure, match="Stale"):
        await flow.engine.dispatch(lease)
    await control.tick()
    completed = await flow.engine.run(SCOPE, execution.id)
    assert completed.state == ExecutionState.SUCCEEDED
    assert completed.snapshot.capability.version_id == execution.snapshot.capability.version_id
    assert await control.recovery.recover() == 0


async def test_waiting_input_recovery_snooze_does_not_answer(control: Control) -> None:
    flow = control.flow
    execution = await waiting(flow)
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "input_request")
    await control.interactions.act(SCOPE, projection.id, InteractionAction.SNOOZE, wake_at=1005)
    assert await control.recovery.recover() == 0
    flow.now[0] = 1005
    await control.tick()
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.SUSPENDED
    async with flow.catalog.sessions() as session:
        assert (await session.scalars(select(InputRequest))).one().state == "pending"
    await control.interactions.act(
        SCOPE, projection.id, InteractionAction.PROVIDE_INPUT, response={"value": 9}
    )
    await control.wakeups.schedule_resume(SCOPE, execution.id, wake_at=1006)
    flow.now[0] = 1006
    await control.tick()
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED


async def test_approval_recovery_and_scheduled_resume(control: Control) -> None:
    flow = control.flow
    version = await flow.publish(graph(WorkflowNode(id="approve", kind="approval")))
    execution = await flow.run(version)
    assert execution.approval_request_id
    assert await control.recovery.recover() == 0
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "approval")
    await control.interactions.act(SCOPE, projection.id, InteractionAction.APPROVE)
    await control.wakeups.schedule_resume(SCOPE, execution.id, wake_at=1001)
    flow.now[0] = 1001
    await control.tick()
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED


@pytest.mark.parametrize("kind", ["approval", "input"])
async def test_request_expiration_closes_waiting_execution(control: Control, kind: str) -> None:
    flow = control.flow
    if kind == "input":
        execution = await waiting(flow)
    else:
        execution = await flow.run(await flow.publish(graph(WorkflowNode(id="approve", kind="approval"))))
    flow.now[0] = 1301
    await control.tick()
    result = await flow.repository.get(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED
    assert result.error == ("input_expired" if kind == "input" else "approval_expired")
    await control.interactions.refresh()
    assert all(
        row.kind == "result" or row.state == "resolved" for row in await control.interactions.list(SCOPE)
    )


async def test_in_app_and_rich_delivery_are_separate_and_literal(control: Control) -> None:
    flow = control.flow
    await waiting(flow)
    await control.interactions.refresh()
    output = StringIO()
    service = NotificationService(
        control.repository, RichNotificationSink(Console(file=output, force_terminal=False))
    )
    assert await service.run_once()
    assert not await service.run_once()
    assert "Supply a value." in output.getvalue()
    async with flow.catalog.sessions() as session:
        rows = list(await session.scalars(select(NotificationDelivery)))
        assert {row.channel for row in rows} == {"in_app", "console"}
        assert all(row.state == "delivered" for row in rows)
    projection = (await control.interactions.list(SCOPE))[0]
    with pytest.raises(NotFound):
        await control.interactions.act(
            SCOPE.model_copy(update={"user_id": "other"}), projection.id, InteractionAction.VIEW
        )
    await control.interactions.act(SCOPE, projection.id, InteractionAction.DISMISS)
    assert (await flow.repository.get(SCOPE, projection.execution_id)).state == ExecutionState.SUSPENDED


async def test_delivery_interrupted_is_reclaimed_not_claimed_exactly_once(control: Control) -> None:
    await waiting(control.flow)
    await control.interactions.refresh()
    first = await control.repository.claim_delivery()
    assert first
    assert await control.repository.claim_delivery() is None
    await control.recovery.recover()
    second = await control.repository.claim_delivery()
    assert second and second.id == first.id and second.token != first.token
    with pytest.raises(RuntimeFailure, match="Stale"):
        await control.repository.finish_delivery(first)
    await control.repository.finish_delivery(second)


async def test_timer_scope_deadline_and_cancellation(control: Control) -> None:
    execution = await control.queued()
    with pytest.raises(RuntimeFailure, match="budget"):
        await control.wakeups.delay(SCOPE, execution.id, wake_at=2000)
    with pytest.raises(NotFound):
        await control.wakeups.delay(
            SCOPE.model_copy(update={"workspace_id": "other"}), execution.id, wake_at=1001
        )
    await control.wakeups.delay(SCOPE, execution.id, wake_at=1001)
    await control.flow.engine.cancel(SCOPE, execution.id)
    control.flow.now[0] = 1001
    await control.tick()
    async with control.flow.catalog.sessions() as session:
        assert (await session.scalars(select(ExecutionWakeup))).one().state == "cancelled"


@pytest.mark.parametrize("state", ["pending", "answered", "consumed", "expired"])
async def test_phase9_input_facts_migrate_preserving_legacy_interaction_id(
    control: Control, state: str
) -> None:
    flow = control.flow
    execution = await waiting(flow)
    if state in {"answered", "consumed"}:
        await answer(flow, execution)
        if state == "consumed":
            await flow.engine.resume(SCOPE, execution.id)
            await flow.engine.run(SCOPE, execution.id)
    elif state == "expired":
        flow.now[0] = 1301
        await control.tick()
    async with flow.catalog.sessions() as session:
        await session.execute(text("DELETE FROM notification_delivery"))
        await session.execute(text("DELETE FROM notification"))
        await session.execute(text("DROP TABLE interaction"))
        await session.execute(text("CREATE TABLE interaction AS SELECT * FROM input_request"))
        await session.execute(text("DROP TABLE input_request"))
        await session.execute(
            text("CREATE INDEX idx_interaction_owner ON interaction(workspace_id,user_id,state)")
        )
        await session.execute(text("DELETE FROM schema_migration WHERE version=13"))
        await session.commit()
    await upgrade_database(flow.catalog.sessions.kw["bind"])
    await upgrade_database(flow.catalog.sessions.kw["bind"])
    async with flow.catalog.sessions() as session:
        fact = (await session.scalars(select(InputRequest))).one()
        assert fact.state == state
        assert fact.response == ({"value": 11} if state in {"answered", "consumed"} else None)
        projection = (await session.scalars(select(Interaction))).one()
        assert projection.kind == "input_request" and projection.source_id == projection.id
    await control.interactions.refresh()
    if state == "pending":
        assert await control.repository.claim_delivery()
        assert (await answer(flow, execution)).state == "answered"


@pytest.mark.parametrize("crash", [False, True])
async def test_non_idempotent_external_effect_is_never_blindly_replayed(
    secured: SecureKernel, crash: bool
) -> None:
    repository = SQLiteExecutionControlRepository(secured.catalog.sessions, clock=lambda: secured.now[0])
    wakeups = ExecutionWakeupService(repository)
    secured.engine.wakeups = wakeups
    executed: list[str] = []

    async def external(context: ExecutorContext) -> ExecutorResult:
        executed.append(context.execution.id)
        raise TransientExecutionError("Response lost after external effect.")

    secured.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"external_write": external})
    execution = await pending(secured, "external_write")
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    if crash:
        lease = await secured.engine.start(SCOPE, execution.id)
        await secured.repository.dispatch(lease, secured.invocations.security)
        executed.append(execution.id)
        await RecoveryService(repository, secured.invocations.registry.retry_safe).recover()
        await wakeups.run_due()
        result = await secured.repository.get(SCOPE, execution.id)
        assert result.wait_reason == "interrupted" and not result.control["replay_safe"]
        with pytest.raises(RuntimeFailure, match="reconciliation"):
            await secured.engine.resume(SCOPE, execution.id)
        for schedule in (wakeups.delay, wakeups.snooze, wakeups.schedule_resume):
            with pytest.raises(RuntimeFailure, match="reconciliation"):
                await schedule(SCOPE, execution.id, wake_at=1001)
    else:
        result = await secured.engine.run(SCOPE, execution.id)
        assert result.state == ExecutionState.FAILED and result.error == "unsafe_retry_denied"
    await RecoveryService(repository, secured.invocations.registry.retry_safe).recover()
    await wakeups.run_due()
    assert len(executed) == 1
    async with secured.catalog.sessions() as session:
        assert not (await session.scalars(select(ExecutionWakeup))).all()


async def test_completed_non_idempotent_effect_remains_terminal(secured: SecureKernel) -> None:
    execution = await pending(secured, "external_write")
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    completed = await secured.engine.run(SCOPE, execution.id)
    assert completed.state == ExecutionState.SUCCEEDED and len(secured.executed) == 1
    repository = SQLiteExecutionControlRepository(secured.catalog.sessions, clock=lambda: secured.now[0])
    assert await RecoveryService(repository, secured.invocations.registry.retry_safe).recover() == 0
    with pytest.raises(RuntimeFailure):
        await secured.engine.run(SCOPE, execution.id)
    assert len(secured.executed) == 1


async def test_retry_policy_is_frozen_and_bounded(control: Control) -> None:
    flow = control.flow

    async def fail(_context: ExecutorContext) -> ExecutorResult:
        raise TransientExecutionError("Temporary.")

    flow.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"echo": fail})
    control.wakeups.policy = RetryPolicy(max_attempts=2, initial_seconds=8, maximum_seconds=2)
    execution = await control.queued()
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.control["wake_at"] == 1002
    control.wakeups.policy = RetryPolicy(max_attempts=10)
    flow.now[0] = 1002
    await control.tick()
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED and result.attempts == 2


async def test_child_retry_wakes_parent_using_the_same_engine(control: Control) -> None:
    flow, calls = control.flow, 0

    async def once(context: ExecutorContext) -> ExecutorResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransientExecutionError("Temporary.")
        return ExecutorResult(output=context.execution.input)

    flow.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"echo": once})
    child = await flow.function()
    from test_workflow import binding

    version = await flow.publish(
        graph(
            WorkflowNode(
                id="call",
                kind="function",
                capability=reference(child),
                input_mapping={"value": binding("input", "value")},
            ),
            children=(child,),
        )
    )
    result = await flow.run(version)
    assert result.wait_reason == "waiting_for_child"
    flow.now[0] = 1001
    await control.tick()
    result = await flow.engine.run(SCOPE, result.id)
    assert result.state == ExecutionState.SUCCEEDED and calls == 2
    assert result.usage.child_executions == 1


async def test_child_expiration_takes_workflow_error_edge(control: Control) -> None:
    flow = control.flow
    child = await flow.publish(
        CapabilityContent(
            implementation=SkillImplementation(instructions="No real model."),
            required_permissions=(READ,),
            permission_boundary=ALL,
            approval_requirement="always",
        )
    )
    version = await flow.publish(
        graph(
            WorkflowNode(id="call", kind="skill", capability=reference(child)),
            children=(child,),
            edges=(
                WorkflowEdge(source="start", target="call"),
                WorkflowEdge(source="call", target="end"),
                WorkflowEdge(source="call", target="end", label="error"),
            ),
        )
    )
    execution = await flow.invocations.invoke(
        Invocation(
            capability=reference(version),
            context=InvocationContext(scope=SCOPE, actor_id="owner", permission_ceiling=ALL),
            input={"value": 7},
            budget=ExecutionBudget(max_duration_seconds=1000),
        )
    )
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.wait_reason == "waiting_for_child"
    flow.now[0] = 1301
    await control.tick()
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED


async def test_due_resume_rechecks_permissions(control: Control) -> None:
    flow = control.flow
    execution = await control.queued()
    await control.wakeups.delay(SCOPE, execution.id, wake_at=1001)
    flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ()})
    flow.now[0] = 1001
    await control.tick()
    assert await flow.engine.run_once()
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.FAILED
    assert not flow.calls


async def test_completed_external_child_is_not_replayed_after_parent_crash(secured: SecureKernel) -> None:
    child = await capability(secured, "external_write")
    content = CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=(
                WorkflowNode(id="start", kind="input"),
                WorkflowNode(id="call", kind="function", capability=reference(child)),
                WorkflowNode(id="end", kind="output"),
            ),
            edges=(WorkflowEdge(source="start", target="call"), WorkflowEdge(source="call", target="end")),
        ),
        required_permissions=(READ,),
        permission_boundary=ALL,
        risk_profile=RiskProfile(level="high"),
        side_effect_profile=SideEffectProfile(kind="external_write"),
        allowed_capabilities=(reference(child),),
        dependency_lock=DependencyLock(dependencies=(locked(child),)),
    )
    draft = await secured.catalog.service.create_draft(SCOPE, CapabilityKind.WORKFLOW, "Recovery", content)
    version = await secured.catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)
    from test_security_core import request

    execution = await secured.invocations.invoke(request(version))
    result = await secured.engine.run(SCOPE, execution.id)
    await approve(secured, result)
    await secured.engine.resume(SCOPE, result.id)
    lease = await secured.engine.start(SCOPE, result.id)
    await secured.repository.dispatch(lease, secured.invocations.security)
    controls = WorkflowService(
        SQLiteWorkflowRepository(secured.catalog.sessions, clock=lambda: secured.now[0]),
        secured.invocations.security,
    )
    secured.engine._workflow_context = controls.bind
    await controls.repository.save(
        lease, WorkflowCheckpoint(node_id="start", charged=True), secured.invocations.security
    )
    await controls.repository.save(
        lease,
        WorkflowCheckpoint(node_id="call", charged=True, values={"start": {}}),
        secured.invocations.security,
    )
    child_execution = await secured.invocations.invoke_child(
        lease, ChildInvocation(capability=reference(child), call_key="call")
    )
    child_pending = await secured.engine.run(SCOPE, child_execution.id)
    await approve(secured, child_pending)
    await secured.engine.resume(SCOPE, child_execution.id)
    assert (await secured.engine.run(SCOPE, child_execution.id)).state == ExecutionState.SUCCEEDED
    repository = SQLiteExecutionControlRepository(secured.catalog.sessions, clock=lambda: secured.now[0])
    await RecoveryService(repository, secured.invocations.registry.retry_safe).recover()
    await repository.tick()
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUSPENDED
    await approve(secured, result)
    await secured.engine.resume(SCOPE, result.id)
    result = await secured.engine.run(SCOPE, result.id)
    assert result.state == ExecutionState.SUCCEEDED and secured.executed == [child_execution.id]


async def test_snooze_before_first_delivery_reminds_only_once(control: Control) -> None:
    await waiting(control.flow)
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "input_request")
    await control.interactions.act(SCOPE, projection.id, InteractionAction.SNOOZE, wake_at=1005)
    assert await control.repository.claim_delivery() is None
    control.flow.now[0] = 1005
    await control.tick()
    delivery = await control.repository.claim_delivery()
    assert delivery
    await control.repository.finish_delivery(delivery)
    assert await control.repository.claim_delivery() is None


async def test_failed_snooze_rolls_back_projection_and_timer(control: Control) -> None:
    await waiting(control.flow)
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "input_request")
    with pytest.raises(RuntimeFailure, match="budget"):
        await control.interactions.act(SCOPE, projection.id, InteractionAction.SNOOZE, wake_at=2000)
    assert (await control.repository.get(SCOPE, projection.id)).snoozed_until is None
    async with control.flow.catalog.sessions() as session:
        assert not (await session.scalars(select(ExecutionWakeup))).all()


async def test_cancelled_execution_closes_input_projection(control: Control) -> None:
    execution = await waiting(control.flow)
    await control.interactions.refresh()
    await control.flow.engine.cancel(SCOPE, execution.id)
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "input_request")
    assert projection.state == "resolved" and projection.actions == (InteractionAction.VIEW,)
    async with control.flow.catalog.sessions() as session:
        fact = await session.get(InputRequest, projection.source_id)
        assert fact and fact.state == "pending"
    delivery = await control.repository.claim_delivery()
    assert delivery and delivery.interaction_id != projection.id
    await control.repository.finish_delivery(delivery)
    assert await control.repository.claim_delivery() is None


async def test_console_delivery_failure_has_bounded_retry(control: Control) -> None:
    await waiting(control.flow)
    sink = AsyncMock()
    sink.deliver.side_effect = OSError("Unlogged transport details.")
    service = NotificationService(control.repository, sink)
    for now in (1000, 1002, 1006):
        control.flow.now[0] = now
        assert await service.run_once()
        assert not await service.run_once()
    control.flow.now[0] = 1100
    assert not await service.run_once()
    assert sink.deliver.await_count == 3
    async with control.flow.catalog.sessions() as session:
        delivery = (
            await session.scalars(
                select(NotificationDelivery).where(NotificationDelivery.channel == "console")
            )
        ).one()
        assert delivery.state == "failed" and delivery.error == "notification_delivery_failed"


async def test_immediate_resume_with_real_clock_does_not_reject_elapsed_microseconds(
    control: Control,
) -> None:
    execution = await control.queued()
    service = ExecutionWakeupService(SQLiteExecutionControlRepository(control.flow.catalog.sessions))
    await service.schedule_resume(SCOPE, execution.id, wake_at=time.time())
    assert await service.run_due() == 1
    assert (await control.flow.repository.get(SCOPE, execution.id)).state == ExecutionState.QUEUED


def test_wakeup_result_cannot_silently_complete_instead_of_suspending() -> None:
    with pytest.raises(ValidationError, match="must suspend"):
        ExecutorResult(wake_at=1005)


async def test_execution_maintenance_worker_queues_due_timer(runtime: Runtime) -> None:
    scope = runtime.platform.scope
    service = CapabilityService(runtime.capabilities, authorizer=AsyncMock())
    draft = await service.create_draft(
        scope,
        CapabilityKind.SKILL,
        "Wakeup",
        CapabilityContent(
            implementation=SkillImplementation(instructions="No model call."),
        ),
    )
    version = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    execution = await runtime.invocations.invoke(
        Invocation(
            capability=reference(version),
            context=InvocationContext(scope=scope, actor_id="owner"),
        )
    )
    await runtime.wakeups.schedule_resume(scope, execution.id, wake_at=time.time())
    runtime.wake.clear()
    stopping = asyncio.Event()
    task = asyncio.create_task(ExecutionMaintenanceWorker(lambda: runtime).execute_async(stopping))
    try:
        await asyncio.wait_for(runtime.wake.wait(), timeout=5)
        assert (await runtime.invocations.repository.get(scope, execution.id)).state == ExecutionState.QUEUED
    finally:
        stopping.set()
        await task


@pytest.mark.parametrize("kind", ["approval", "input"])
async def test_human_response_automatically_resumes_but_respects_snooze(control: Control, kind: str) -> None:
    flow = control.flow
    execution = (
        await waiting(flow)
        if kind == "input"
        else await flow.run(await flow.publish(graph(WorkflowNode(id="approve", kind="approval"))))
    )
    await control.interactions.refresh()
    projection = next(
        row
        for row in await control.interactions.list(SCOPE)
        if row.kind == ("input_request" if kind == "input" else "approval")
    )
    await control.interactions.act(SCOPE, projection.id, InteractionAction.SNOOZE, wake_at=1005)
    if kind == "input":
        await control.interactions.act(
            SCOPE, projection.id, InteractionAction.PROVIDE_INPUT, response={"value": 8}
        )
    else:
        await control.interactions.act(SCOPE, projection.id, InteractionAction.APPROVE)
    await control.recovery.recover()
    await control.tick()
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.SUSPENDED
    flow.now[0] = 1005
    await control.tick()
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.QUEUED
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED


async def test_committed_input_response_is_discovered_without_explicit_resume(control: Control) -> None:
    flow = control.flow
    execution = await waiting(flow)
    await answer(flow, execution)
    await control.recovery.recover()
    await control.tick()
    assert (await flow.repository.get(SCOPE, execution.id)).state == ExecutionState.QUEUED
    assert (await flow.engine.run(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED


@pytest.mark.parametrize("revoke", [False, True])
async def test_child_approval_automatically_wakes_root_and_rechecks_gates(
    control: Control, revoke: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_workflow import binding

    flow = control.flow
    original = await flow.function()
    contracts = flow.invocations.registry._functions
    monkeypatch.setitem(
        contracts, "echo", contracts["echo"].model_copy(update={"approval_requirement": "always"})
    )
    child = await flow.publish(original.content.model_copy(update={"approval_requirement": "always"}))
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="call",
                kind="function",
                capability=reference(child),
                input_mapping={"value": binding("input", "value")},
            ),
            children=(child,),
        )
    )
    execution = await flow.run(version)
    assert execution.wait_reason == "waiting_for_child"
    await control.interactions.refresh()
    projection = next(row for row in await control.interactions.list(SCOPE) if row.kind == "approval")
    assert projection.execution_id != execution.id
    await control.interactions.act(SCOPE, projection.id, InteractionAction.APPROVE)
    if revoke:
        flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ()})
    await control.tick()
    assert await flow.engine.run_once()
    result = await flow.repository.get(SCOPE, execution.id)
    assert result.state == (ExecutionState.FAILED if revoke else ExecutionState.SUCCEEDED)
    assert len(flow.calls) == (0 if revoke else 1)
