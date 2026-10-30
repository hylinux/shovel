from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shovel.capability.domain import (
    AgentImplementation,
    Availability,
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    CapabilityScope,
    CapabilityVersion,
    ConnectionRequirement,
    DependencyLock,
    FunctionImplementation,
    Permission,
    RiskProfile,
    SideEffectProfile,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.capability.service import CapabilityService
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.model.runtime import Operation
from shovel.execution.domain import (
    BudgetExceeded,
    BudgetUsage,
    ChildInvocation,
    Execution,
    ExecutionBudget,
    ExecutionLease,
    ExecutionState,
    ExecutionStateMachine,
    ExecutorResult,
    Invocation,
    InvocationContext,
)
from shovel.execution.engine import ExecutionEngine
from shovel.execution.executors import AgentExecutor, FunctionExecutor, SkillExecutor, WorkflowExecutor
from shovel.execution.invocation import InvocationService
from shovel.execution.ports import ExecutorContext
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.maintenance import prune_history
from shovel.runtime.schemas import HistoryPruneInput
from shovel.runtime.service import Runtime
from test_capability_registry import SCOPE, Catalog, ExplicitPublisher, locked, skill
from test_capability_registry import catalog as catalog
from test_hosted_runtime import runtime as runtime


@dataclass
class Kernel:
    catalog: Catalog
    repository: SQLiteExecutionRepository
    invocations: InvocationService
    engine: ExecutionEngine
    functions: FunctionExecutor
    now: list[float]


async def echo(context: ExecutorContext) -> ExecutorResult:
    return ExecutorResult(output={"echo": context.execution.input})


@pytest.fixture
async def kernel(catalog: Catalog) -> AsyncIterator[Kernel]:
    now = [1000.0]
    repository = SQLiteExecutionRepository(catalog.sessions, clock=lambda: now[0])
    invocations = InvocationService(catalog.registry, repository)
    functions = FunctionExecutor({"demo.read": echo})
    engine = ExecutionEngine(
        invocations,
        {
            CapabilityKind.FUNCTION: functions,
            CapabilityKind.SKILL: SkillExecutor(),
            CapabilityKind.WORKFLOW: WorkflowExecutor(),
            CapabilityKind.AGENT: AgentExecutor(lambda: pytest.fail("No network client expected.")),
        },
        clock=lambda: now[0],
    )
    try:
        yield Kernel(catalog, repository, invocations, engine, functions, now)
    finally:
        await engine.close()


async def publish(catalog: Catalog, content: CapabilityContent | None = None) -> CapabilityVersion:
    content = content or CapabilityContent(
        implementation=FunctionImplementation(implementation_id="demo.read")
    )
    draft = await catalog.service.create_draft(
        SCOPE, CapabilityKind(content.implementation.kind), "Execution test", content
    )
    return await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


def reference(version: CapabilityVersion) -> CapabilityReference:
    return CapabilityReference(capability_id=version.capability_id, version=version.number)


def invocation(version: CapabilityVersion, *, scope: CapabilityScope = SCOPE, **kwargs: Any) -> Invocation:
    return Invocation(
        capability=reference(version),
        context=InvocationContext(scope=scope, actor_id=scope.user_id),
        **kwargs,
    )


async def parent_and_child(
    kernel: Kernel, budget: ExecutionBudget | None = None
) -> tuple[ExecutionLease, CapabilityVersion]:
    child = await publish(kernel.catalog)
    parent = await publish(
        kernel.catalog,
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id="demo.read"),
            allowed_capabilities=(reference(child),),
            dependency_lock=DependencyLock(dependencies=(locked(child),)),
        ),
    )
    execution = await kernel.invocations.invoke(invocation(parent, budget=budget or ExecutionBudget()))
    return await kernel.engine.start(SCOPE, execution.id), child


async def test_create_dispatch_complete_in_existing_operation_store(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version, input={"value": 4}))
    lease = await kernel.engine.start(SCOPE, execution.id)
    assert lease.execution.state == ExecutionState.RUNNING
    completed = await kernel.engine.dispatch(lease)
    assert completed.state == ExecutionState.SUCCEEDED and completed.output == {"echo": {"value": 4}}
    assert completed.usage == BudgetUsage(steps=1, function_calls=1)
    attempts = await kernel.repository.attempts(SCOPE, execution.id)
    assert len(attempts) == 1 and attempts[0].outcome == ExecutionState.SUCCEEDED
    assert attempts[0].dispatched_at == kernel.now[0]
    assert attempts[0].finished_at == kernel.now[0]
    async with kernel.catalog.sessions() as session:
        row = await session.get(Operation, execution.id)
        assert row is not None and row.capability_version_id == version.id
        tables = set(await session.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
        assert "execution_attempt" in tables
        assert (
            not {
                "execution",
                "proactive_task",
                "workflow_task",
                "skill_task",
                "agent_task",
                "function_task",
            }
            & tables
        )
    with pytest.raises(RuntimeFailure, match="Stale"):
        await kernel.engine.dispatch(lease)
    with pytest.raises(RuntimeFailure, match="Illegal"):
        await kernel.engine.start(SCOPE, execution.id)
    with pytest.raises(RuntimeFailure, match="Illegal"):
        await kernel.engine.resume(SCOPE, execution.id)


@pytest.mark.parametrize("target", list(ExecutionState))
@pytest.mark.parametrize("current", list(ExecutionState))
def test_state_machine_rejects_all_illegal_transitions(
    current: ExecutionState, target: ExecutionState
) -> None:
    allowed = {
        "queued": {"running", "suspended", "failed", "cancelled"},
        "running": {"suspended", "succeeded", "failed", "cancelled"},
        "suspended": {"queued", "failed", "cancelled"},
    }
    if target in allowed.get(current, set()):
        ExecutionStateMachine.check(current, target)
    else:
        with pytest.raises(RuntimeFailure, match="Illegal"):
            ExecutionStateMachine.check(current, target)


async def test_request_replay_and_conflict_do_not_resolve_a_new_version(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    request = invocation(version, request_key="lost-response", input={"value": 1})
    first, concurrent = await asyncio.gather(
        kernel.invocations.invoke(request), kernel.invocations.invoke(request)
    )
    assert first.id == concurrent.id and first.snapshot == concurrent.snapshot
    await kernel.catalog.service.set_availability(
        SCOPE, version.capability_id, Availability.DISABLED, number=1
    )
    assert await kernel.invocations.invoke(request) == first
    with pytest.raises(RuntimeFailure, match="another intent"):
        await kernel.invocations.invoke(invocation(version, request_key="lost-response", input={"value": 2}))
    with pytest.raises(RuntimeFailure, match="unavailable"):
        await kernel.engine.start(SCOPE, first.id)
    assert await kernel.engine.run_once(first.id)
    assert (await kernel.repository.get(SCOPE, first.id)).state == ExecutionState.FAILED


async def test_version_and_dependency_lock_remain_frozen_after_new_publication(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    original = parent.execution.snapshot
    for version in (
        dependency,
        await kernel.catalog.registry.get_version(SCOPE, original.capability.capability_id, 1),
    ):
        draft = await kernel.catalog.service.create_draft_from_version(SCOPE, version.capability_id, 1)
        await kernel.catalog.service.update_draft(
            SCOPE,
            version.capability_id,
            2,
            draft.content.model_copy(update={"provenance": {"revision": "new"}}),
            expected_revision=1,
        )
        await kernel.catalog.service.publish(SCOPE, version.capability_id, 2, expected_revision=2)
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="child")
    )
    assert child.snapshot.capability.version_id == dependency.id
    assert child.snapshot.capability_call_path == (*original.capability_call_path, reference(dependency))
    with pytest.raises(RuntimeFailure, match="not declared"):
        await kernel.invocations.invoke_child(
            parent,
            ChildInvocation(
                capability=CapabilityReference(capability_id=dependency.capability_id, version=2),
                call_key="changed-version",
            ),
        )
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).snapshot == original
    await kernel.engine.run(SCOPE, child.id)
    completed = await kernel.engine.dispatch(parent)
    assert completed.state == ExecutionState.SUCCEEDED and completed.snapshot == original


async def test_parent_child_identity_idempotency_scope_and_budget_propagation(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    request = ChildInvocation(capability=reference(dependency), call_key="first", input={"value": 1})
    child, duplicate = await asyncio.gather(
        kernel.invocations.invoke_child(parent, request),
        kernel.invocations.invoke_child(parent, request),
    )
    assert child.id == duplicate.id
    assert child.root_execution_id == parent.execution.id and child.parent_execution_id == parent.execution.id
    assert child.correlation_id == parent.execution.correlation_id
    assert child.snapshot.context.permission_ceiling == parent.execution.snapshot.context.permission_ceiling
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).usage.child_executions == 1
    with pytest.raises(RuntimeFailure, match="another intent"):
        await kernel.invocations.invoke_child(
            parent, ChildInvocation(capability=reference(dependency), call_key="first", input={"value": 2})
        )
    other_scope = CapabilityScope(workspace_id="other", user_id=SCOPE.user_id)
    with pytest.raises(NotFound):
        await kernel.repository.get(other_scope, child.id)
    with pytest.raises(RuntimeFailure, match="unfinished child"):
        await kernel.engine.succeed(parent, {})
    completed = await kernel.engine.run(SCOPE, child.id)
    assert completed.usage == BudgetUsage(steps=1, function_calls=1)
    usage = (await kernel.repository.get(SCOPE, parent.execution.id)).usage
    assert usage == BudgetUsage(steps=2, child_executions=1, function_calls=1)
    assert (await kernel.engine.dispatch(parent)).state == ExecutionState.SUCCEEDED
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).usage.function_calls == 2


@pytest.mark.parametrize(
    ("budget", "error"),
    [
        (ExecutionBudget(max_child_executions=0), "child_executions"),
        (ExecutionBudget(max_call_depth=1), "call depth"),
    ],
)
async def test_child_budget_and_depth_are_enforced(
    kernel: Kernel, budget: ExecutionBudget, error: str
) -> None:
    parent, dependency = await parent_and_child(kernel, budget)
    with pytest.raises(RuntimeFailure, match=error):
        await kernel.invocations.invoke_child(
            parent, ChildInvocation(capability=reference(dependency), call_key="blocked")
        )
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).usage.child_executions == 0


async def test_child_budget_cannot_expand_and_recursive_calls_are_rejected(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel, ExecutionBudget(max_steps=4))
    with pytest.raises(RuntimeFailure, match="enlarge"):
        await kernel.invocations.invoke_child(
            parent,
            ChildInvocation(
                capability=reference(dependency), call_key="larger", budget=ExecutionBudget(max_steps=5)
            ),
        )
    for number in (1, 2):
        with pytest.raises(RuntimeFailure, match="Recursive"):
            await kernel.invocations.invoke_child(
                parent,
                ChildInvocation(
                    capability=CapabilityReference(
                        capability_id=parent.execution.snapshot.capability.capability_id, version=number
                    ),
                    call_key=f"recursive-{number}",
                ),
            )


@pytest.mark.parametrize("counter", ["steps", "agent_turns", "function_calls", "child_executions"])
async def test_all_budget_counters_are_atomic_and_ancestor_cumulative(kernel: Kernel, counter: str) -> None:
    parent, dependency = await parent_and_child(kernel)
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="counter")
    )
    lease = await kernel.engine.start(SCOPE, child.id)
    parent_before = await kernel.repository.get(SCOPE, parent.execution.id)
    child_before = await kernel.repository.get(SCOPE, child.id)
    limit = getattr(parent_before.snapshot.budget, f"max_{counter}")
    delta = limit - getattr(parent_before.usage, counter)
    await kernel.repository.consume(lease, BudgetUsage.model_validate({counter: delta}))
    before = await kernel.repository.get(SCOPE, parent.execution.id)
    with pytest.raises(BudgetExceeded, match=counter):
        await kernel.repository.consume(lease, BudgetUsage.model_validate({counter: 1}))
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).usage == before.usage
    after_child = await kernel.repository.get(SCOPE, child.id)
    assert getattr(after_child.usage, counter) == getattr(child_before.usage, counter) + delta


async def test_function_call_budget_blocks_handler(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(
        invocation(version, budget=ExecutionBudget(max_function_calls=0))
    )
    completed = await kernel.engine.run(SCOPE, execution.id)
    assert completed.state == ExecutionState.FAILED and completed.error == "budget_exceeded"
    assert completed.usage.function_calls == 0


async def test_concurrent_start_and_duplicate_dispatch_are_fenced(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    outcomes = await asyncio.gather(
        kernel.engine.start(SCOPE, execution.id),
        kernel.engine.start(SCOPE, execution.id),
        return_exceptions=True,
    )
    lease = next(item for item in outcomes if isinstance(item, ExecutionLease))
    assert sum(isinstance(item, RuntimeFailure) for item in outcomes) == 1
    results = await asyncio.gather(
        kernel.engine.dispatch(lease), kernel.engine.dispatch(lease), return_exceptions=True
    )
    assert sum(isinstance(item, Execution) for item in results) == 1
    assert sum(isinstance(item, RuntimeFailure) for item in results) == 1
    assert len(await kernel.repository.attempts(SCOPE, execution.id)) == 1
    assert (await kernel.repository.get(SCOPE, execution.id)).usage.function_calls == 1


async def test_suspend_resume_preserves_snapshot_budget_and_deadline(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    lease = await kernel.engine.start(SCOPE, execution.id)
    suspended = await kernel.engine.suspend(lease, {"checkpoint": 1})
    assert suspended.state == ExecutionState.SUSPENDED and suspended.output == {"checkpoint": 1}
    resumed = await kernel.engine.resume(SCOPE, execution.id)
    assert resumed.snapshot == suspended.snapshot and resumed.output == suspended.output
    assert resumed.deadline_at == suspended.deadline_at
    with pytest.raises(RuntimeFailure, match="Stale"):
        await kernel.engine.succeed(lease, {})
    completed = await kernel.engine.run(SCOPE, execution.id)
    assert completed.state == ExecutionState.SUCCEEDED and completed.usage.steps == 2
    assert [attempt.outcome for attempt in await kernel.repository.attempts(SCOPE, execution.id)] == [
        ExecutionState.SUSPENDED,
        ExecutionState.SUCCEEDED,
    ]


async def test_expired_suspended_execution_cannot_reset_duration(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(
        invocation(version, budget=ExecutionBudget(max_duration_seconds=1))
    )
    lease = await kernel.engine.start(SCOPE, execution.id)
    await kernel.engine.suspend(lease)
    kernel.now[0] += 2
    with pytest.raises(BudgetExceeded, match="duration"):
        await kernel.engine.resume(SCOPE, execution.id)
    assert (await kernel.repository.get(SCOPE, execution.id)).state == ExecutionState.SUSPENDED
    await kernel.engine.cancel(SCOPE, execution.id)


async def test_cancel_cascades_and_late_completion_cannot_overwrite(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="cancel")
    )
    child_lease = await kernel.engine.start(SCOPE, child.id)
    assert (await kernel.engine.cancel(SCOPE, parent.execution.id)).state == ExecutionState.CANCELLED
    assert (await kernel.repository.get(SCOPE, child.id)).state == ExecutionState.CANCELLED
    with pytest.raises(RuntimeFailure, match="Stale"):
        await kernel.engine.succeed(child_lease, {})
    assert (await kernel.repository.attempts(SCOPE, child.id))[0].outcome == ExecutionState.CANCELLED


async def test_fail_cancels_descendants_and_sanitizes_error_identifiers(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="failure")
    )
    with pytest.raises(RuntimeFailure, match="bounded identifier"):
        await kernel.engine.fail(parent, error_code="secret message")
    failed = await kernel.engine.fail(parent, error_code="test_failed")
    assert failed.state == ExecutionState.FAILED and failed.error == "test_failed"
    assert (await kernel.repository.get(SCOPE, child.id)).state == ExecutionState.CANCELLED


async def test_version_disabled_between_start_and_dispatch_fails_without_stuck_claim(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    lease = await kernel.engine.start(SCOPE, execution.id)
    await kernel.catalog.service.set_availability(
        SCOPE, version.capability_id, Availability.DISABLED, number=1
    )
    failed = await kernel.engine.dispatch(lease)
    assert failed.state == ExecutionState.FAILED and failed.error == "version_unavailable"
    assert failed.usage.function_calls == 0


async def test_disabled_locked_dependency_blocks_parent_dispatch(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    await kernel.catalog.service.set_availability(
        SCOPE, dependency.capability_id, Availability.DISABLED, number=1
    )
    result = await kernel.engine.dispatch(parent)
    assert result.state == ExecutionState.FAILED and result.error == "version_unavailable"
    assert result.usage.function_calls == 0


async def test_executor_child_calls_share_engine_and_parent_budget(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, dependency = await parent_and_child(kernel)
    children: list[Execution] = []

    async def handler(context: ExecutorContext) -> ExecutorResult:
        if context.execution.parent_execution_id is None:
            children.append(
                await context.call(
                    ChildInvocation(capability=reference(dependency), call_key="nested", input={"value": 7})
                )
            )
            return ExecutorResult(output={"child": children[0].output})
        return await echo(context)

    monkeypatch.setitem(kernel.functions._functions, "demo.read", handler)
    result = await kernel.engine.dispatch(parent)
    assert result.state == ExecutionState.SUCCEEDED and result.output == {"child": {"echo": {"value": 7}}}
    assert children[0].state == ExecutionState.SUCCEEDED
    assert result.usage == BudgetUsage(steps=2, child_executions=1, function_calls=2)


async def test_executor_can_suspend_and_resume_without_replacing_version(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(context: ExecutorContext) -> ExecutorResult:
        if context.execution.attempts == 1:
            return ExecutorResult(output={"checkpoint": 1}, suspended=True)
        assert context.execution.output == {"checkpoint": 1}
        return ExecutorResult(output={"done": True})

    monkeypatch.setitem(kernel.functions._functions, "demo.read", handler)
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    suspended = await kernel.engine.run(SCOPE, execution.id)
    assert suspended.state == ExecutionState.SUSPENDED
    await kernel.engine.resume(SCOPE, execution.id)
    result = await kernel.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and result.output == {"done": True}
    assert result.snapshot == suspended.snapshot and result.deadline_at == suspended.deadline_at
    assert result.usage == BudgetUsage(steps=2, function_calls=2)


async def test_child_start_charges_ancestor_steps_atomically(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel, ExecutionBudget(max_steps=1))
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="no-steps")
    )
    with pytest.raises(BudgetExceeded, match="steps"):
        await kernel.engine.start(SCOPE, child.id)
    assert (await kernel.repository.get(SCOPE, child.id)).state == ExecutionState.QUEUED
    assert not await kernel.repository.attempts(SCOPE, child.id)
    assert (await kernel.repository.get(SCOPE, parent.execution.id)).usage.steps == 1


async def test_child_duration_never_exceeds_parent_deadline(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel, ExecutionBudget(max_duration_seconds=3))
    kernel.now[0] += 2
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="bounded-time")
    )
    lease = await kernel.engine.start(SCOPE, child.id)
    assert lease.execution.deadline_at == parent.execution.deadline_at
    kernel.now[0] += 2
    with pytest.raises(BudgetExceeded, match="duration"):
        await kernel.engine.suspend(lease)
    assert (await kernel.engine.cancel(SCOPE, parent.execution.id)).state == ExecutionState.CANCELLED


async def test_cancel_before_start_never_creates_attempt(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    assert (await kernel.engine.cancel(SCOPE, execution.id)).state == ExecutionState.CANCELLED
    assert not await kernel.repository.attempts(SCOPE, execution.id)
    assert not await kernel.engine.run_once(execution.id)


async def test_agent_turn_budget_is_checked_before_model_client(kernel: Kernel) -> None:
    version = await publish(
        kernel.catalog, CapabilityContent(implementation=AgentImplementation(instructions="Answer briefly"))
    )
    execution = await kernel.invocations.invoke(
        invocation(version, budget=ExecutionBudget(max_agent_turns=0))
    )
    result = await kernel.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED and result.error == "budget_exceeded"
    assert result.usage.agent_turns == 0


async def test_input_validation_uses_frozen_schema_before_creating_execution(kernel: Kernel) -> None:
    version = await publish(
        kernel.catalog,
        CapabilityContent(
            implementation=AgentImplementation(instructions="Answer briefly"),
            input_schema={"type": "object", "required": ["question"]},
        ),
    )
    with pytest.raises(RuntimeFailure, match="frozen JSON Schema"):
        await kernel.invocations.invoke(invocation(version))


@pytest.mark.parametrize("invalid_output", [False, True])
async def test_executor_failure_and_output_validation_do_not_persist_exception_bodies(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], invalid_output: bool
) -> None:
    async def broken(_context: ExecutorContext) -> ExecutorResult:
        if invalid_output:
            return ExecutorResult(output="wrong output type")
        raise ValueError("private-secret-content")

    monkeypatch.setitem(kernel.functions._functions, "demo.read", broken)
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    failed = await kernel.engine.run(SCOPE, execution.id)
    assert failed.state == ExecutionState.FAILED and failed.error == "executor_failed"
    assert "private-secret-content" not in capsys.readouterr().out
    assert failed.output is None


@pytest.mark.parametrize("mode", ["cancel", "caller_cancel", "timeout", "close"])
async def test_inflight_executor_is_interrupted_and_claim_is_closed(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def blocked(_context: ExecutorContext) -> ExecutorResult:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return ExecutorResult(output={})

    monkeypatch.setitem(kernel.functions._functions, "demo.read", blocked)
    version = await publish(kernel.catalog)
    budget = ExecutionBudget(max_duration_seconds=0.1 if mode == "timeout" else 300)
    execution = await kernel.invocations.invoke(invocation(version, budget=budget))
    running = asyncio.create_task(kernel.engine.run(SCOPE, execution.id))
    await asyncio.wait_for(entered.wait(), 3)
    if mode == "cancel":
        await kernel.engine.cancel(SCOPE, execution.id)
    elif mode == "caller_cancel":
        running.cancel()
    elif mode == "close":
        await kernel.engine.close()
    if mode == "caller_cancel":
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        await asyncio.wait_for(running, 3)
    assert stopped.is_set()
    current = await kernel.repository.get(SCOPE, execution.id)
    assert current.state == (ExecutionState.FAILED if mode == "timeout" else ExecutionState.CANCELLED)
    assert (await kernel.repository.attempts(SCOPE, execution.id))[0].outcome == current.state


@pytest.mark.parametrize(
    "content",
    [
        skill().model_copy(update={"risk_profile": RiskProfile(level="high")}),
        skill().model_copy(update={"side_effect_profile": SideEffectProfile(kind="external_write")}),
        skill().model_copy(update={"approval_requirement": "always"}),
        skill().model_copy(
            update={
                "required_permissions": (Permission(action="read", resource="documents"),),
                "permission_boundary": (Permission(action="read", resource="documents"),),
            }
        ),
        skill().model_copy(
            update={
                "connection_requirements": (ConnectionRequirement(name="mail", connection_ref="outlook"),),
            }
        ),
    ],
)
async def test_default_security_policy_requires_approval_or_denies_unconfigured_permissions(
    kernel: Kernel, content: CapabilityContent
) -> None:
    version = await publish(kernel.catalog, content)
    if content.risk_profile.level == "high" or content.approval_requirement == "always":
        execution = await kernel.invocations.invoke(invocation(version))
        pending = await kernel.engine.run(SCOPE, execution.id)
        assert pending.state == ExecutionState.SUSPENDED and pending.approval_request_id is not None
        assert pending.usage == BudgetUsage(steps=1)
    else:
        with pytest.raises(RuntimeFailure):
            await kernel.invocations.invoke(invocation(version))


@pytest.mark.parametrize("kind", [CapabilityKind.SKILL, CapabilityKind.WORKFLOW])
async def test_skeleton_executors_fail_explicitly_without_parallel_lifecycles(
    kernel: Kernel, kind: CapabilityKind
) -> None:
    content = (
        skill()
        if kind == CapabilityKind.SKILL
        else CapabilityContent(
            implementation=WorkflowImplementation(
                nodes=(WorkflowNode(id="input", kind="input"), WorkflowNode(id="output", kind="output")),
                edges=(WorkflowEdge(source="input", target="output"),),
            )
        )
    )
    version = await publish(kernel.catalog, content)
    execution = await kernel.invocations.invoke(invocation(version))
    failed = await kernel.engine.run(SCOPE, execution.id)
    assert failed.state == ExecutionState.FAILED and failed.error == "executor_failed"
    assert failed.usage == BudgetUsage(steps=1)


async def test_sql_guards_protect_snapshot_lock_input_identity_and_terminal_state(kernel: Kernel) -> None:
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))
    updates = [
        "execution_data = '{}'",
        "payload = '[]'",
        "capability_version_id = NULL",
        "correlation_id = 'other'",
        "root_execution_id = 'other'",
        "state = 'succeeded'",
    ]
    for update in updates:
        async with kernel.catalog.sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(f"UPDATE operation SET {update} WHERE id=:id"), {"id": execution.id}
                )
            await session.rollback()
    async with kernel.catalog.sessions() as session:
        columns = list(Operation.__table__.columns.keys())
        with pytest.raises(IntegrityError, match="cannot be replaced"):
            await session.execute(
                text(
                    f"INSERT OR REPLACE INTO operation({','.join(columns)}) "
                    f"SELECT {','.join(columns)} FROM operation WHERE id=:id"
                ),
                {"id": execution.id},
            )
        await session.rollback()
    assert (await kernel.repository.get(SCOPE, execution.id)).snapshot == execution.snapshot


async def test_sql_replace_cannot_rebind_child_call_identity(kernel: Kernel) -> None:
    parent, dependency = await parent_and_child(kernel)
    child = await kernel.invocations.invoke_child(
        parent, ChildInvocation(capability=reference(dependency), call_key="stable")
    )
    columns = list(Operation.__table__.columns.keys())
    values = [":replacement" if name == "id" else name for name in columns]
    async with kernel.catalog.sessions() as session:
        with pytest.raises(IntegrityError, match="cannot be replaced"):
            await session.execute(
                text(
                    f"INSERT OR REPLACE INTO operation({','.join(columns)}) "
                    f"SELECT {','.join(values)} FROM operation WHERE id=:id"
                ),
                {"id": child.id, "replacement": "replacement-child"},
            )
        await session.rollback()
    assert (
        await kernel.invocations.invoke_child(
            parent, ChildInvocation(capability=reference(dependency), call_key="stable")
        )
    ).id == child.id


async def test_close_waits_for_dispatch_cleanup_not_the_callers_lifetime(
    kernel: Kernel, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, returned, release_caller = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(_context: ExecutorContext) -> ExecutorResult:
        entered.set()
        await asyncio.Event().wait()
        return ExecutorResult(output={})

    monkeypatch.setitem(kernel.functions._functions, "demo.read", blocked)
    version = await publish(kernel.catalog)
    execution = await kernel.invocations.invoke(invocation(version))

    async def caller() -> None:
        await kernel.engine.run(SCOPE, execution.id)
        returned.set()
        await release_caller.wait()

    running = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(kernel.engine.close(), 3)
        await asyncio.wait_for(returned.wait(), 3)
        assert not running.done()
        assert (await kernel.repository.get(SCOPE, execution.id)).state == ExecutionState.CANCELLED
    finally:
        release_caller.set()
        await asyncio.wait_for(running, 3)


async def test_host_routes_through_same_engine_and_preserves_legacy_recovery_and_pruning(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = runtime.platform.scope
    service = CapabilityService(runtime.capabilities, authorizer=ExplicitPublisher(scope))
    draft = await service.create_draft(scope, CapabilityKind.SKILL, "Host test", skill())
    version = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    execution = await runtime.invocations.invoke(invocation(version, scope=scope))

    async def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Versioned execution must not enter the legacy operation executor.")

    monkeypatch.setattr(runtime.operations, "_execute", forbidden)
    assert await runtime.operations.run_once()
    assert (await runtime.invocations.repository.get(scope, execution.id)).state == ExecutionState.FAILED
    with pytest.raises(RuntimeFailure, match="legacy Operation retries"):
        await runtime.operations.retry(execution.id)
    with pytest.raises(RuntimeFailure, match="InvocationService"):
        await runtime.operations.enqueue("capability.invoke", {})
    another = await runtime.invocations.invoke(invocation(version, scope=scope))
    lease = await runtime.execution_engine.start(scope, another.id)
    await runtime.operations.recover()
    assert (await runtime.invocations.repository.get(scope, another.id)).state == ExecutionState.RUNNING
    await runtime.execution_engine.cancel(scope, another.id)
    async with runtime.sessions() as session:
        row = await session.get(Operation, execution.id)
        assert row is not None
        row.finished_at = 1
        session.add_all(
            [
                Operation(id="prune", kind="history.prune", state="running"),
                Operation(id="old", kind="cleanup", state="succeeded", finished_at=1),
            ]
        )
        await session.commit()
    counts = await prune_history(runtime, HistoryPruneInput(keep_days=1), "prune")
    assert counts["operations"] == 1
    assert (await runtime.invocations.repository.get(scope, execution.id)).state == ExecutionState.FAILED
    assert (await runtime.invocations.repository.attempts(scope, lease.execution.id))[0].outcome == (
        ExecutionState.CANCELLED
    )


async def test_agent_executor_uses_frozen_instructions_without_workspace_tools(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = runtime.platform.scope
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": "Answer"}}]}
        )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("Execution adapter must not load mutable main-agent extensions.")

    monkeypatch.setattr(runtime.chat.agent.extensions, "load", forbidden)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service = CapabilityService(runtime.capabilities, authorizer=ExplicitPublisher(scope))
    content = CapabilityContent(implementation=AgentImplementation(instructions="Frozen capability rules"))
    draft = await service.create_draft(scope, CapabilityKind.AGENT, "Agent", content)
    version = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    execution = await runtime.invocations.invoke(
        invocation(version, scope=scope, input={"question": "hello"})
    )
    result = await runtime.execution_engine.run(scope, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and result.output == {"text": "Answer"}
    assert result.usage.agent_turns == 1 and len(requests) == 1
    assert "Frozen capability rules" in json.dumps(requests)
    assert not requests[0].get("tools")


async def test_legacy_operations_remain_unversioned_with_nullable_snapshots(kernel: Kernel) -> None:
    async with kernel.catalog.sessions() as session:
        session.add(Operation(id="legacy", kind="cleanup"))
        await session.commit()
    async with kernel.catalog.sessions() as session:
        legacy = await session.get(Operation, "legacy")
        assert legacy is not None and legacy.execution_data is None
    assert await kernel.repository.queued("legacy") is None
