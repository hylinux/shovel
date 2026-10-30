from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import httpx
import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from shovel.automation.domain import NormalizedEvent
from shovel.automation.events import InProcessEventBus
from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityKind,
    CapabilityScope,
    CapabilityVersion,
    Condition,
    DependencyLock,
    FunctionContract,
    FunctionImplementation,
    InputBinding,
    RiskProfile,
    SideEffectProfile,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.interaction import InputRequest as InputRow
from shovel.db.model.interaction import Notification
from shovel.db.model.runtime import Operation
from shovel.db.model.security import ApprovalRequest as ApprovalRow
from shovel.db.workflow_repository import SQLiteWorkflowRepository
from shovel.execution.domain import (
    BudgetExceeded,
    Execution,
    ExecutionBudget,
    ExecutionState,
    ExecutorResult,
    Invocation,
    InvocationContext,
)
from shovel.execution.engine import ExecutionEngine
from shovel.execution.executors import AgentExecutor, FunctionExecutor, SkillExecutor, WorkflowExecutor
from shovel.execution.invocation import InvocationService
from shovel.execution.ports import ExecutorContext
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.service import Runtime
from shovel.security.domain import ApprovalPreference, SecurityDenied
from shovel.security.services import ApprovalService, AuthorizationService, SecurityGate
from shovel.workflow.domain import WorkflowCheckpoint
from shovel.workflow.service import WorkflowService
from test_capability_registry import SCOPE, Catalog, ExplicitPublisher, locked
from test_capability_registry import catalog as catalog
from test_execution_engine import reference
from test_hosted_runtime import runtime as runtime
from test_security_core import ALL, READ, Human, Policies

VALUE: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


class InputHuman:
    async def authorize(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, user_id: str
    ) -> None:
        assert scope == SCOPE and user_id == SCOPE.user_id and execution_id and interaction_id


@dataclass
class Flow:
    catalog: Catalog
    service: CapabilityService
    repository: SQLiteExecutionRepository
    invocations: InvocationService
    engine: ExecutionEngine
    controls: WorkflowService
    approvals: ApprovalService
    policies: Policies
    now: list[float]
    calls: list[Execution]
    bus: InProcessEventBus

    async def publish(self, content: CapabilityContent) -> CapabilityVersion:
        draft = await self.service.create_draft(
            SCOPE, CapabilityKind(content.implementation.kind), "Workflow test", content
        )
        return await self.service.publish(SCOPE, draft.capability_id, draft.number, expected_revision=1)

    async def function(self, identifier: str = "echo") -> CapabilityVersion:
        return await self.publish(
            CapabilityContent(
                implementation=FunctionImplementation(implementation_id=identifier),
                input_schema=VALUE,
                output_schema=VALUE,
                required_permissions=(READ,),
                permission_boundary=ALL,
            )
        )

    async def run(self, version: CapabilityVersion, **kwargs: Any) -> Execution:
        execution = await self.invocations.invoke(
            Invocation(
                capability=reference(version),
                context=InvocationContext(
                    scope=SCOPE, actor_id=SCOPE.user_id, permission_ceiling=ALL, **kwargs
                ),
                input={"value": 7},
            )
        )
        return await self.engine.run(SCOPE, execution.id)

    async def resume(self, execution: Execution) -> Execution:
        await self.engine.resume(SCOPE, execution.id)
        return await self.engine.run(SCOPE, execution.id)


@pytest.fixture
async def flow(catalog: Catalog) -> AsyncIterator[Flow]:
    contracts = tuple(
        FunctionContract(
            implementation_id=name,
            input_schema=VALUE,
            output_schema=VALUE,
            required_permissions=(READ,),
        )
        for name in ("echo", "fail")
    )
    registry = CapabilityRegistry(catalog.repository, functions=contracts)
    service = CapabilityService(registry, authorizer=catalog.publisher)
    policies, now, calls = Policies(), [1000.0], []
    gate = SecurityGate(AuthorizationService(policies))
    repository = SQLiteExecutionRepository(catalog.sessions, clock=lambda: now[0])
    invocations = InvocationService(registry, repository, security=gate)
    bus = InProcessEventBus()
    controls = WorkflowService(
        SQLiteWorkflowRepository(catalog.sessions, clock=lambda: now[0]),
        gate,
        authorizer=InputHuman(),
        bus=bus,
    )

    async def echo(context: ExecutorContext) -> ExecutorResult:
        calls.append(context.execution)
        return ExecutorResult(output=context.execution.input)

    async def fail(context: ExecutorContext) -> ExecutorResult:
        raise RuntimeFailure("Expected test failure.")

    engine = ExecutionEngine(
        invocations,
        {
            CapabilityKind.FUNCTION: FunctionExecutor({"echo": echo, "fail": fail}),
            CapabilityKind.SKILL: SkillExecutor(),
            CapabilityKind.WORKFLOW: WorkflowExecutor(),
            CapabilityKind.AGENT: AgentExecutor(lambda: pytest.fail("No real model calls.")),
        },
        clock=lambda: now[0],
        workflow_context=controls.bind,
    )
    approvals = ApprovalService(
        SQLiteApprovalRepository(catalog.sessions, clock=lambda: now[0]), gate, authorizer=Human()
    )
    await bus.start()
    try:
        yield Flow(
            catalog, service, repository, invocations, engine, controls, approvals, policies, now, calls, bus
        )
    finally:
        await bus.stop()
        await engine.close()


def binding(*path: str) -> InputBinding:
    return InputBinding(source="context", path=path)


def graph(
    *nodes: WorkflowNode,
    children: tuple[CapabilityVersion, ...] = (),
    edges: tuple[WorkflowEdge, ...] | None = None,
    output_schema: dict[str, JsonValue] | None = None,
    local_write: bool = False,
) -> CapabilityContent:
    sequence = (
        WorkflowNode(id="start", kind="input"),
        *nodes,
        WorkflowNode(
            id="end",
            kind="output",
            input_mapping={"value": binding("input", "value")},
        ),
    )
    return CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=sequence,
            edges=edges
            if edges is not None
            else tuple(WorkflowEdge(source=left.id, target=right.id) for left, right in pairwise(sequence)),
        ),
        input_schema=VALUE,
        output_schema=output_schema or VALUE,
        required_permissions=(READ,),
        permission_boundary=ALL,
        allowed_capabilities=tuple(reference(child) for child in children),
        dependency_lock=DependencyLock(dependencies=tuple(locked(child) for child in children)),
        risk_profile=RiskProfile(level="medium" if local_write else "low"),
        side_effect_profile=SideEffectProfile(kind="local_write" if local_write else "none"),
    )


async def test_simple_workflow_and_child_exact_version_permission_freeze(flow: Flow) -> None:
    child = await flow.function()
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="read",
                kind="function",
                capability=reference(child),
                permission_boundary=(READ,),
                input_mapping={"value": binding("input", "value")},
            ),
            children=(child,),
        )
    )
    await flow.service.create_draft_from_version(SCOPE, child.capability_id, 1)
    result = await flow.run(version)
    assert result.state == ExecutionState.SUCCEEDED and result.output == {"value": 7}
    assert result.usage.child_executions == 1 and len(flow.calls) == 1
    invoked = flow.calls[0]
    assert invoked.parent_execution_id == result.id and invoked.root_execution_id == result.id
    assert invoked.correlation_id == result.correlation_id
    assert invoked.snapshot.capability.number == 1 and invoked.snapshot.granted_permissions == (READ,)
    assert result.snapshot.capability.content.dependency_lock == version.content.dependency_lock


@pytest.mark.parametrize("value,branch", [(7, "yes"), (0, "no")])
async def test_condition_branch_and_convergence(flow: Flow, value: int, branch: str) -> None:
    content = graph(
        WorkflowNode(
            id="check", kind="condition", condition=Condition(path=("input", "value"), operator="gt", value=1)
        ),
        WorkflowNode(id="yes", kind="approval"),
        WorkflowNode(id="no", kind="approval"),
        edges=tuple(
            WorkflowEdge(source=source, target=target, label=label)
            for source, target, label in (
                ("start", "check", "next"),
                ("check", "yes", "true"),
                ("check", "no", "false"),
                ("yes", "end", "next"),
                ("no", "end", "next"),
            )
        ),
    )
    version = await flow.publish(content)
    execution = await flow.invocations.invoke(
        Invocation(
            capability=reference(version),
            context=InvocationContext(scope=SCOPE, actor_id="owner", permission_ceiling=ALL),
            input={"value": value},
        )
    )
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.checkpoint and result.checkpoint["node_id"] == branch
    assert result.wait_reason == "waiting_for_approval"


async def test_mandatory_approval_nodes_pause_at_node_and_are_single_use(flow: Flow) -> None:
    version = await flow.publish(
        graph(
            WorkflowNode(id="first", kind="approval"),
            WorkflowNode(id="second", kind="approval"),
        )
    )
    result = await flow.run(version, proactive_preference=ApprovalPreference.MINIMUM_REQUIRED)
    assert result.state == ExecutionState.SUSPENDED and result.checkpoint
    assert "start" in result.checkpoint["values"] and result.attempts == 1
    for node_id in ("first", "second"):
        assert result.checkpoint and result.checkpoint["node_id"] == node_id
        assert result.approval_request_id
        request_id = result.approval_request_id
        pending = await flow.approvals.get(SCOPE, result.id, request_id, user_id=SCOPE.user_id)
        assert "workflow_approval" in pending.reasons
        await flow.approvals.approve(SCOPE, result.id, request_id, user_id=SCOPE.user_id)
        result = await flow.resume(result)
        with pytest.raises(RuntimeFailure):
            await flow.approvals.approve(SCOPE, result.id, request_id, user_id=SCOPE.user_id)
    assert result.state == ExecutionState.SUCCEEDED
    async with flow.catalog.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ApprovalRow).where(ApprovalRow.state == "consumed")
            )
            == 2
        )


async def waiting(flow: Flow) -> Execution:
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="answer",
                kind="ask_user",
                prompt="Supply a value.",
                response_schema=VALUE,
            )
        )
    )
    result = await flow.run(version)
    assert result.wait_reason == "waiting_for_input" and isinstance(result.output, dict)
    return result


async def answer(flow: Flow, execution: Execution, response: JsonValue = None):
    assert isinstance(execution.output, dict)
    return await flow.controls.respond(
        SCOPE,
        execution.id,
        str(execution.output["interaction_id"]),
        user_id=SCOPE.user_id,
        response=response if response is not None else {"value": 11},
    )


async def test_input_binding_replay_schema_and_explicit_resume(flow: Flow) -> None:
    result = await waiting(flow)
    result = await flow.resume(result)
    assert result.state == ExecutionState.SUSPENDED
    with pytest.raises(RuntimeFailure, match="JSON Schema"):
        await answer(flow, result, {"value": "bad"})
    assert (await answer(flow, result)).state == "answered"
    assert (await flow.repository.get(SCOPE, result.id)).state == ExecutionState.SUSPENDED
    with pytest.raises(RuntimeFailure, match="replay"):
        await answer(flow, result)
    result = await flow.resume(result)
    assert result.state == ExecutionState.SUCCEEDED
    assert result.checkpoint and result.checkpoint["values"]["answer"] == {"value": 11}
    async with flow.catalog.sessions() as session:
        assert (await session.scalars(select(InputRow))).one().state == "consumed"


async def test_input_wrong_user_execution_workspace_and_untrusted_response(flow: Flow) -> None:
    left, right = await waiting(flow), await waiting(flow)
    assert isinstance(left.output, dict)
    identifier = str(left.output["interaction_id"])
    with pytest.raises(SecurityDenied):
        await flow.controls.respond(SCOPE, left.id, identifier, user_id="other", response={})
    with pytest.raises(NotFound):
        await flow.controls.respond(SCOPE, right.id, identifier, user_id=SCOPE.user_id, response={})
    other = SCOPE.model_copy(update={"workspace_id": "other"})
    with pytest.raises(NotFound):
        await flow.controls.respond(other, left.id, identifier, user_id=SCOPE.user_id, response={})
    denied = WorkflowService(flow.controls.repository, flow.controls.gate)
    with pytest.raises(RuntimeFailure, match="Trusted input"):
        await denied.respond(SCOPE, left.id, identifier, user_id=SCOPE.user_id, response={})


async def test_input_expiry_and_permission_revocation(flow: Flow) -> None:
    result = await waiting(flow)
    flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ()})
    with pytest.raises(SecurityDenied):
        await answer(flow, result)
    flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ALL})
    flow.now[0] += 301
    with pytest.raises(RuntimeFailure, match="expired"):
        await answer(flow, result)
    with pytest.raises(BudgetExceeded):
        await flow.resume(result)
    async with flow.catalog.sessions() as session:
        request = (await session.scalars(select(InputRow))).one()
        assert request.state == "expired" and request.expires_at == result.deadline_at


async def test_input_response_is_immutable_in_storage(flow: Flow) -> None:
    result = await waiting(flow)
    await answer(flow, result)
    async with flow.catalog.sessions() as session:
        request = (await session.scalars(select(InputRow))).one()
        request.response = {"value": 12}
        with pytest.raises(IntegrityError, match="immutable"):
            await session.commit()


async def test_checkpoint_restores_frozen_version_and_answer_after_recomposition(flow: Flow) -> None:
    content = graph(WorkflowNode(id="answer", kind="ask_user", prompt="Value?", response_schema=VALUE))
    implementation = content.implementation
    assert isinstance(implementation, WorkflowImplementation)
    end = implementation.nodes[-1].model_copy(
        update={"input_mapping": {"value": binding("nodes", "answer", "value")}}
    )
    content = content.model_copy(
        update={
            "implementation": implementation.model_copy(update={"nodes": (*implementation.nodes[:-1], end)})
        }
    )
    version = await flow.publish(content)
    result = await flow.run(version)
    await answer(flow, result)
    draft = await flow.service.create_draft_from_version(SCOPE, version.capability_id, 1)
    changed = graph()
    draft = await flow.service.update_draft(
        SCOPE, draft.capability_id, draft.number, changed, expected_revision=1
    )
    await flow.service.publish(SCOPE, draft.capability_id, draft.number, expected_revision=draft.revision)
    restored = await flow.repository.get(SCOPE, result.id)
    await flow.engine.close()
    fresh = ExecutionEngine(
        flow.invocations,
        {
            CapabilityKind.FUNCTION: FunctionExecutor({}),
            CapabilityKind.SKILL: SkillExecutor(),
            CapabilityKind.WORKFLOW: WorkflowExecutor(),
            CapabilityKind.AGENT: AgentExecutor(lambda: pytest.fail("No real model.")),
        },
        clock=lambda: flow.now[0],
        workflow_context=flow.controls.bind,
    )
    try:
        await fresh.resume(SCOPE, result.id)
        completed = await fresh.run(SCOPE, result.id)
        assert completed.state == ExecutionState.SUCCEEDED and completed.output == {"value": 11}
        assert completed.snapshot.capability.number == 1
        assert completed.snapshot.capability.content_hash == restored.snapshot.capability.content_hash
    finally:
        await fresh.close()


async def test_concurrent_responses_accept_one_and_checkpoint_consumption_rolls_back(flow: Flow) -> None:
    result = await waiting(flow)
    replies = await asyncio.gather(
        answer(flow, result, {"value": 1}), answer(flow, result, {"value": 2}), return_exceptions=True
    )
    assert sum(isinstance(reply, RuntimeFailure) for reply in replies) == 1
    assert sum(not isinstance(reply, BaseException) for reply in replies) == 1
    await flow.engine.resume(SCOPE, result.id)
    lease = await flow.engine.start(SCOPE, result.id)
    # A bad advance must not consume the accepted response.
    with pytest.raises(RuntimeFailure, match="cannot be skipped"):
        await flow.controls.repository.save(
            lease, WorkflowCheckpoint(node_id="end", values={"answer": {"value": 3}}), flow.controls.gate
        )
    async with flow.catalog.sessions() as session:
        assert (await session.scalars(select(InputRow))).one().state == "answered"
    await flow.engine.cancel(SCOPE, result.id)


async def test_approval_rechecks_permissions_before_continuation(flow: Flow) -> None:
    version = await flow.publish(graph(WorkflowNode(id="approval", kind="approval")))
    result = await flow.run(version)
    assert result.approval_request_id
    await flow.approvals.approve(SCOPE, result.id, result.approval_request_id, user_id=SCOPE.user_id)
    flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ()})
    await flow.engine.resume(SCOPE, result.id)
    with pytest.raises(SecurityDenied):
        await flow.engine.run(SCOPE, result.id)


async def test_preflight_and_explicit_node_approvals_use_distinct_attempts(flow: Flow) -> None:
    flow.policies.value = flow.policies.value.model_copy(update={"preference": ApprovalPreference.ALWAYS})
    version = await flow.publish(graph(WorkflowNode(id="approval", kind="approval")))
    result = await flow.run(version)
    for _ in range(2):
        assert result.state == ExecutionState.SUSPENDED and result.approval_request_id
        await flow.approvals.approve(SCOPE, result.id, result.approval_request_id, user_id=SCOPE.user_id)
        result = await flow.resume(result)
    assert result.state == ExecutionState.SUCCEEDED


async def test_waiting_cancellation_and_step_budget(flow: Flow) -> None:
    result = await waiting(flow)
    await flow.engine.cancel(SCOPE, result.id)
    with pytest.raises(RuntimeFailure, match="not waiting"):
        await answer(flow, result)
    version = await flow.publish(graph())
    execution = await flow.invocations.invoke(
        Invocation(
            capability=reference(version),
            context=InvocationContext(scope=SCOPE, actor_id="owner", permission_ceiling=ALL),
            input={"value": 1},
            budget=ExecutionBudget(max_steps=2),
        )
    )
    result = await flow.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED and result.error == "budget_exceeded"


async def test_phase_eight_database_upgrade_adds_checkpoint_without_another_execution(flow: Flow) -> None:
    async with flow.catalog.sessions() as session:
        session.add(Operation(id="pre9", kind="cleanup", result={"preserved": True}))
        await session.commit()
        await session.execute(text("DROP TABLE interaction"))
        await session.execute(text("DROP TABLE notification"))
        await session.execute(text("ALTER TABLE operation DROP COLUMN workflow_data"))
        await session.execute(text("DELETE FROM schema_migration WHERE version=12"))
        await session.commit()
    engine = flow.catalog.sessions.kw["bind"]
    await upgrade_database(engine)
    await upgrade_database(engine)
    async with flow.catalog.sessions() as session:
        operation = await session.get(Operation, "pre9")
        assert operation and operation.result == {"preserved": True} and operation.workflow_data is None
        assert await session.scalar(text("SELECT count(*) FROM schema_migration WHERE version=12")) == 1
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()


async def test_capability_cycle_cannot_cross_versions(flow: Flow) -> None:
    original = await flow.publish(graph())
    child = await flow.publish(
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id="echo"),
            input_schema=VALUE,
            output_schema=VALUE,
            required_permissions=(READ,),
            permission_boundary=ALL,
            allowed_capabilities=(reference(original),),
            dependency_lock=DependencyLock(dependencies=(locked(original),)),
        )
    )
    content = graph(
        WorkflowNode(
            id="call",
            kind="function",
            capability=reference(child),
            input_mapping={"value": binding("input", "value")},
        ),
        children=(child,),
    )
    content = content.model_copy(
        update={"dependency_lock": DependencyLock(dependencies=(locked(child), locked(original)))}
    )
    draft = await flow.service.create_draft_from_version(SCOPE, original.capability_id, 1)
    draft = await flow.service.update_draft(SCOPE, draft.capability_id, 2, content, expected_revision=1)
    with pytest.raises(RuntimeFailure, match="cycles"):
        await flow.service.publish(SCOPE, draft.capability_id, 2, expected_revision=draft.revision)


async def test_notification_and_event_have_separate_local_records(flow: Flow) -> None:
    received: list[NormalizedEvent] = []

    async def handler(event: NormalizedEvent) -> None:
        received.append(event)

    await flow.bus.stop()
    flow.bus.subscribe(handler)
    await flow.bus.start()
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="notice",
                kind="notification",
                prompt="Completed.",
                input_mapping={"value": binding("input", "value")},
            ),
            WorkflowNode(
                id="emit",
                kind="emit_event",
                event_type="workflow.completed",
                input_mapping={"value": binding("input", "value")},
            ),
            local_write=True,
        )
    )
    result = await flow.run(version)
    await flow.bus.stop()
    assert result.state == ExecutionState.SUCCEEDED and len(received) == 1
    assert received[0].event_type == "workflow.completed" and received[0].payload == {"value": 7}
    async with flow.catalog.sessions() as session:
        notification = (await session.scalars(select(Notification))).one()
        assert notification.execution_id == result.id and notification.payload == {"value": 7}
        assert not (await session.scalars(select(InputRow))).all()
        assert not (await session.scalars(select(ApprovalRow))).all()


@pytest.mark.parametrize("kind", ["notification", "emit_event"])
async def test_local_effects_recheck_live_authorization(flow: Flow, kind: str) -> None:
    received: list[NormalizedEvent] = []

    async def record(event: NormalizedEvent) -> None:
        received.append(event)

    await flow.bus.stop()
    flow.bus.subscribe(record)
    await flow.bus.start()
    child = await flow.function()

    async def revoke(context: ExecutorContext) -> ExecutorResult:
        flow.policies.value = flow.policies.value.model_copy(update={"user_grants": ()})
        return ExecutorResult(output=context.execution.input)

    flow.engine._executors[CapabilityKind.FUNCTION] = FunctionExecutor({"echo": revoke})
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="call",
                kind="function",
                capability=reference(child),
                input_mapping={"value": binding("input", "value")},
            ),
            WorkflowNode(id="effect", kind=kind, event_type="test.effect" if kind == "emit_event" else None),
            children=(child,),
            local_write=True,
        )
    )
    result = await flow.run(version)
    await flow.bus.stop()
    assert result.state == ExecutionState.FAILED
    assert not received
    async with flow.catalog.sessions() as session:
        assert not (await session.scalars(select(Notification))).all()


@pytest.mark.parametrize("handle", [False, True])
async def test_child_failure_path(flow: Flow, handle: bool) -> None:
    child = await flow.function("fail")
    edges = [WorkflowEdge(source="start", target="call"), WorkflowEdge(source="call", target="end")]
    if handle:
        edges.append(WorkflowEdge(source="call", target="end", label="error"))
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="call",
                kind="function",
                capability=reference(child),
                input_mapping={"value": binding("input", "value")},
            ),
            children=(child,),
            edges=tuple(edges),
        )
    )
    result = await flow.run(version)
    assert result.state == (ExecutionState.SUCCEEDED if handle else ExecutionState.FAILED)


async def test_suspended_child_uses_same_engine_and_parent_resume(flow: Flow) -> None:
    child = await flow.publish(
        CapabilityContent(
            implementation=SkillImplementation(instructions="Frozen skill"),
            required_permissions=(READ,),
            permission_boundary=ALL,
            approval_requirement="always",
        )
    )
    version = await flow.publish(
        graph(
            WorkflowNode(
                id="skill",
                kind="skill",
                capability=reference(child),
            ),
            children=(child,),
        )
    )
    parent = await flow.run(version)
    assert parent.wait_reason == "waiting_for_child" and parent.checkpoint
    identifier = str(parent.checkpoint["child_execution_id"])
    execution = await flow.repository.get(SCOPE, identifier)
    assert execution.approval_request_id
    assert await flow.repository.queued(identifier) is None
    await flow.approvals.approve(SCOPE, identifier, execution.approval_request_id, user_id=SCOPE.user_id)
    # The unconfigured test skill fails explicitly after the same engine consumes approval.
    result = await flow.resume(parent)
    assert result.state == ExecutionState.FAILED
    assert (await flow.repository.get(SCOPE, identifier)).attempts == 2


@pytest.mark.parametrize(
    "problem",
    [
        "unreachable",
        "cycle",
        "mapping",
        "condition",
        "boundary",
        "risk",
        "output",
        "schema",
    ],
)
async def test_publish_validation(flow: Flow, problem: str) -> None:
    child = await flow.function()
    node = WorkflowNode(
        id="call",
        kind="function",
        capability=reference(child),
        input_mapping={"value": binding("input", "value")},
    )
    content = graph(node, children=(child,))
    data = content.model_dump(mode="json")
    implementation = data["implementation"]
    if problem == "unreachable":
        implementation["nodes"].append({"id": "orphan", "kind": "approval"})
        implementation["edges"].append({"source": "orphan", "target": "end"})
    elif problem == "cycle":
        implementation["edges"].append({"source": "call", "target": "call"})
    elif problem == "mapping":
        implementation["nodes"][1]["input_mapping"]["value"]["path"] = ["nodes", "end", "value"]
    elif problem == "condition":
        implementation["nodes"][1]["condition"] = {"path": ["input", "value"], "operator": "gt", "value": 1}
    elif problem == "boundary":
        implementation["nodes"][1]["permission_boundary"] = []
    elif problem == "risk":
        implementation["nodes"][1] = {"id": "call", "kind": "notification"}
    elif problem == "output":
        data["output_schema"] = {"type": "object", "required": ["unmapped"]}
    elif problem == "schema":
        data["output_schema"] = {"required": [[]]}
    draft = await flow.service.create_draft(
        SCOPE, CapabilityKind.WORKFLOW, "Invalid", CapabilityContent.model_validate(data)
    )
    with pytest.raises(RuntimeFailure, match="Invalid"):
        await flow.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


@pytest.mark.parametrize("state", ["draft", "disabled", "missing"])
async def test_dependency_must_be_available_and_published(flow: Flow, state: str) -> None:
    child = await flow.function()
    content = graph(
        WorkflowNode(
            id="call",
            kind="function",
            capability=reference(child),
            input_mapping={"value": binding("input", "value")},
        ),
        children=(child,),
    )
    if state == "disabled":
        await flow.service.set_availability(SCOPE, child.capability_id, Availability.DISABLED)
    elif state == "draft":
        draft = await flow.service.create_draft_from_version(SCOPE, child.capability_id, 1)
        data = content.model_dump(mode="json")
        for collection in ("allowed_capabilities",):
            data[collection][0]["version"] = draft.number
        data["implementation"]["nodes"][1]["capability"]["version"] = draft.number
        data["dependency_lock"]["dependencies"][0]["version"] = draft.number
        content = CapabilityContent.model_validate(data)
    else:
        data = content.model_dump(mode="json")
        data["dependency_lock"]["dependencies"] = []
        content = CapabilityContent.model_validate(data)
    draft = await flow.service.create_draft(SCOPE, CapabilityKind.WORKFLOW, "Invalid dependency", content)
    with pytest.raises(RuntimeFailure):
        await flow.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


@pytest.mark.parametrize("kind", ["parallel", "join", "loop", "script", "schedule", "event", "cron"])
def test_trigger_and_unsupported_nodes_absent(kind: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowNode(id="invalid", kind=kind)
    with pytest.raises(ValidationError):
        WorkflowImplementation(nodes=(), trigger={"kind": kind})


def test_old_published_workflow_content_keeps_its_hash() -> None:
    content = graph()
    raw = content.model_dump(mode="json")
    raw["implementation"] = {
        "kind": "workflow",
        "nodes": [
            {"id": "start", "kind": "input", "capability": None},
            {"id": "end", "kind": "output", "capability": None},
        ],
        "edges": [{"source": "start", "target": "end"}],
    }
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    restored = CapabilityContent.model_validate(raw)
    assert restored.content_hash == digest and restored.model_dump(mode="json") == raw


async def test_skill_node_uses_frozen_tool_free_agent(
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
        pytest.fail("Skills must not load mutable main-agent extensions.")

    monkeypatch.setattr(runtime.chat.agent.extensions, "load", forbidden)
    runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service = CapabilityService(runtime.capabilities, authorizer=ExplicitPublisher(scope))
    draft = await service.create_draft(
        scope,
        CapabilityKind.SKILL,
        "Skill",
        CapabilityContent(
            implementation=SkillImplementation(instructions="Frozen skill instructions"),
            output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
    )
    child = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    content = CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=(
                WorkflowNode(id="start", kind="input"),
                WorkflowNode(id="skill", kind="skill", capability=reference(child)),
                WorkflowNode(
                    id="end", kind="output", input_mapping={"text": binding("nodes", "skill", "text")}
                ),
            ),
            edges=(WorkflowEdge(source="start", target="skill"), WorkflowEdge(source="skill", target="end")),
        ),
        output_schema=child.content.output_schema,
        allowed_capabilities=(reference(child),),
        dependency_lock=DependencyLock(dependencies=(locked(child),)),
    )
    draft = await service.create_draft(scope, CapabilityKind.WORKFLOW, "Skill workflow", content)
    version = await service.publish(scope, draft.capability_id, 1, expected_revision=1)
    execution = await runtime.invocations.invoke(
        Invocation(
            capability=reference(version),
            context=InvocationContext(scope=scope, actor_id=scope.user_id),
        )
    )
    result = await runtime.execution_engine.run(scope, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and result.output == {"text": "Answer"}
    assert result.usage.agent_turns == 1 and len(requests) == 1
    assert "Frozen skill instructions" in json.dumps(requests) and not requests[0].get("tools")
