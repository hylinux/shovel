from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityScope,
    CapabilityVersion,
    DependencyLock,
    FunctionContract,
    FunctionImplementation,
    Permission,
    RiskProfile,
    SideEffectProfile,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.execution import ExecutionAttempt
from shovel.db.model.security import ApprovalRequest as ApprovalRow
from shovel.execution.domain import (
    BudgetUsage,
    ChildInvocation,
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
from shovel.runtime.requests import request_fingerprint
from shovel.security.domain import (
    ApprovalPreference,
    ApprovalState,
    PermissionSources,
    SecurityDenied,
    UserSecurityPolicy,
)
from shovel.security.services import (
    ApprovalResolver,
    ApprovalService,
    AuthorizationService,
    PermissionResolver,
    RiskEvaluator,
    SecurityGate,
)
from test_capability_registry import SCOPE, Catalog, locked
from test_capability_registry import catalog as catalog
from test_execution_engine import reference

READ = Permission(action="read", resource="documents")
WRITE = Permission(action="write", resource="documents")
ALL = (READ, WRITE)


class Policies:
    def __init__(self) -> None:
        self.value = UserSecurityPolicy(
            user_grants=ALL,
            platform_constraints=ALL,
            allowed_side_effects=("none", "local_write", "external_write", "destructive"),
        )

    async def load(self, scope: CapabilityScope) -> UserSecurityPolicy:
        assert scope == SCOPE
        return self.value


class Human:
    async def authorize(
        self,
        scope: CapabilityScope,
        user_id: str,
        execution_id: str,
        request_id: str,
        action: Literal["approve", "reject"],
    ) -> None:
        assert scope == SCOPE and user_id == SCOPE.user_id and execution_id and request_id


@dataclass
class SecureKernel:
    catalog: Catalog
    policies: Policies
    invocations: InvocationService
    repository: SQLiteExecutionRepository
    approvals: ApprovalService
    engine: ExecutionEngine
    functions: FunctionExecutor
    now: list[float]
    executed: list[str]


@pytest.fixture
async def secured(catalog: Catalog) -> AsyncIterator[SecureKernel]:
    contracts = tuple(
        FunctionContract(
            implementation_id=effect,
            required_permissions=(READ,),
            side_effect_profile=SideEffectProfile(kind=effect),
        )
        for effect in ("none", "local_write", "external_write", "destructive")
    )
    registry = CapabilityRegistry(catalog.repository, functions=contracts)
    catalog = replace(
        catalog, registry=registry, service=CapabilityService(registry, authorizer=catalog.publisher)
    )
    policies, now, executed = Policies(), [1000.0], []
    gate = SecurityGate(AuthorizationService(policies))
    repository = SQLiteExecutionRepository(catalog.sessions, clock=lambda: now[0])
    invocations = InvocationService(registry, repository, security=gate)

    async def record(context: ExecutorContext) -> ExecutorResult:
        executed.append(context.execution.id)
        return ExecutorResult(output={"done": True})

    functions = FunctionExecutor({item.implementation_id: record for item in contracts})
    engine = ExecutionEngine(
        invocations,
        {
            CapabilityKind.FUNCTION: functions,
            CapabilityKind.SKILL: SkillExecutor(),
            CapabilityKind.WORKFLOW: WorkflowExecutor(),
            CapabilityKind.AGENT: AgentExecutor(lambda: pytest.fail("No real model calls.")),
        },
        clock=lambda: now[0],
    )
    approvals = ApprovalService(
        SQLiteApprovalRepository(catalog.sessions, clock=lambda: now[0]),
        gate,
        authorizer=Human(),
    )
    try:
        yield SecureKernel(
            catalog, policies, invocations, repository, approvals, engine, functions, now, executed
        )
    finally:
        await engine.close()


async def capability(
    secured: SecureKernel,
    effect: str = "none",
    *,
    children: tuple[CapabilityVersion, ...] = (),
) -> CapabilityVersion:
    content = CapabilityContent(
        implementation=FunctionImplementation(implementation_id=effect),
        required_permissions=(READ,),
        permission_boundary=ALL,
        side_effect_profile=SideEffectProfile(kind=effect),
        allowed_capabilities=tuple(reference(item) for item in children),
        dependency_lock=DependencyLock(dependencies=tuple(locked(item) for item in children)),
    )
    draft = await secured.catalog.service.create_draft(
        SCOPE, CapabilityKind.FUNCTION, "Security test", content
    )
    return await secured.catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


def request(version: CapabilityVersion, **context: object) -> Invocation:
    return Invocation(
        capability=reference(version),
        context=InvocationContext(scope=SCOPE, actor_id=SCOPE.user_id, permission_ceiling=ALL, **context),
        budget=ExecutionBudget(max_duration_seconds=1000),
    )


async def pending(secured: SecureKernel, effect: str = "external_write") -> Execution:
    execution = await secured.invocations.invoke(request(await capability(secured, effect)))
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUSPENDED and result.approval_request_id is not None
    assert execution.id not in secured.executed
    return result


async def approve(secured: SecureKernel, execution: Execution) -> None:
    assert execution.approval_request_id is not None
    result = await secured.approvals.approve(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert result.state == ApprovalState.APPROVED


@pytest.mark.parametrize(
    "boundary",
    [
        "user_grants",
        "invocation_grants",
        "platform_constraints",
        "capability_boundary",
        "proactive_boundary",
        "workflow_boundary",
        "skill_boundary",
        "parent_permissions",
        "frozen_permissions",
    ],
)
def test_every_permission_source_can_only_narrow(boundary: str) -> None:
    fields = {
        "user_grants": ALL,
        "invocation_grants": ALL,
        "platform_constraints": ALL,
        "capability_boundary": ALL,
    }
    fields[boundary] = (READ,)
    resolved = PermissionResolver().resolve(PermissionSources.model_validate(fields))
    assert resolved == (READ,)
    fields[boundary] = ()
    assert PermissionResolver().resolve(PermissionSources.model_validate(fields)) == ()


def test_permissions_are_exact_not_wildcard_or_resource_prefix_matches() -> None:
    sources = PermissionSources(
        user_grants=(Permission(action="*", resource="*"),),
        invocation_grants=ALL,
        platform_constraints=ALL,
        capability_boundary=ALL,
    )
    assert PermissionResolver().resolve(sources) == ()


async def test_required_permissions_must_be_granted_and_approval_cannot_fill_them(
    secured: SecureKernel,
) -> None:
    secured.policies.value = secured.policies.value.model_copy(
        update={
            "user_grants": (),
            "preference": ApprovalPreference.ALWAYS,
        }
    )
    version = await capability(secured, "external_write")
    with pytest.raises(SecurityDenied, match="Required permissions"):
        await secured.invocations.invoke(request(version))
    async with secured.catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ApprovalRow)) == 0
    assert not secured.executed


async def test_child_permission_narrowing_and_escalation_denial(secured: SecureKernel) -> None:
    child = await capability(secured)
    parent = await capability(secured, children=(child,))
    root = await secured.invocations.invoke(request(parent))
    lease = await secured.engine.start(SCOPE, root.id)
    execution = await secured.invocations.invoke_child(
        lease,
        ChildInvocation(
            capability=reference(child),
            call_key="narrow",
            permission_ceiling=(READ,),
        ),
    )
    assert execution.snapshot.granted_permissions == (READ,)
    assert set(execution.snapshot.granted_permissions) < set(root.snapshot.granted_permissions)
    with pytest.raises(SecurityDenied, match="Required permissions"):
        await secured.invocations.invoke_child(
            lease,
            ChildInvocation(
                capability=reference(child),
                call_key="missing",
                permission_ceiling=(WRITE,),
            ),
        )
    secured.policies.value = secured.policies.value.model_copy(update={"user_grants": (READ,)})
    with pytest.raises(RuntimeFailure, match="enlarge"):
        await secured.invocations.invoke_child(
            lease,
            ChildInvocation(
                capability=reference(child),
                call_key="escalate",
                permission_ceiling=ALL,
            ),
        )
    assert (await secured.repository.get(SCOPE, root.id)).usage.child_executions == 1


async def test_frozen_permissions_do_not_expand_when_user_grants_increase(secured: SecureKernel) -> None:
    secured.policies.value = secured.policies.value.model_copy(update={"user_grants": (READ,)})
    execution = await secured.invocations.invoke(request(await capability(secured)))
    assert execution.snapshot.granted_permissions == (READ,)
    secured.policies.value = secured.policies.value.model_copy(update={"user_grants": ALL})
    evaluation = await secured.invocations.security.evaluate(execution)
    assert evaluation.permissions == (READ,)


@pytest.mark.parametrize(
    ("preference", "risk", "effect", "required"),
    [
        ("ALWAYS", "low", "none", True),
        ("RISK_BASED", "low", "none", False),
        ("RISK_BASED", "medium", "none", True),
        ("EXTERNAL_ACTIONS", "medium", "local_write", False),
        ("EXTERNAL_ACTIONS", "low", "external_write", True),
        ("MINIMUM_REQUIRED", "medium", "none", False),
        ("MINIMUM_REQUIRED", "high", "none", True),
        ("MINIMUM_REQUIRED", "low", "external_write", True),
        ("MINIMUM_REQUIRED", "low", "destructive", True),
    ],
)
def test_approval_preferences_and_platform_minimum(
    preference: str,
    risk: str,
    effect: str,
    required: bool,
) -> None:
    content = CapabilityContent(
        implementation=FunctionImplementation(implementation_id="test"),
        risk_profile=RiskProfile(level=risk),
        side_effect_profile=SideEffectProfile(kind=effect),
    )
    decision = ApprovalResolver().resolve(
        content,
        RiskEvaluator().evaluate(content),
        UserSecurityPolicy(preference=preference),
    )
    assert decision.required is required


@pytest.mark.parametrize("layer", ["capability", "workflow", "platform"])
def test_any_approval_layer_overrides_minimum_required(layer: str) -> None:
    content = CapabilityContent(implementation=FunctionImplementation(implementation_id="test"))
    if layer == "capability":
        content = content.model_copy(update={"approval_requirement": "always"})
    decision = ApprovalResolver().resolve(
        content,
        RiskEvaluator().evaluate(content),
        UserSecurityPolicy(require_approval=layer == "platform"),
        workflow_approval_required=layer == "workflow",
    )
    assert decision.required


def test_never_is_not_a_supported_preference() -> None:
    with pytest.raises(ValidationError):
        UserSecurityPolicy(preference="NEVER")


@pytest.mark.parametrize("effect", ["external_write", "destructive"])
async def test_effect_waits_for_bound_single_use_approval_and_rechecks_before_execution(
    secured: SecureKernel,
    effect: str,
) -> None:
    execution = await pending(secured, effect)
    assert execution.usage == BudgetUsage(steps=1)
    await approve(secured, execution)
    assert not secured.executed
    await secured.engine.resume(SCOPE, execution.id)
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and secured.executed == [execution.id]
    assert result.snapshot == execution.snapshot
    assert result.usage == BudgetUsage(steps=2, function_calls=1)
    receipt = await secured.approvals.get(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert receipt.state == ApprovalState.CONSUMED and receipt.consumed_attempt == 2
    assert receipt.scope == SCOPE and receipt.user_id == SCOPE.user_id
    with pytest.raises(RuntimeFailure, match="replayed"):
        await approve(secured, execution)


@pytest.mark.parametrize("already_approved", [False, True])
async def test_approval_expiration_is_durable_and_cannot_be_approved(
    secured: SecureKernel,
    already_approved: bool,
) -> None:
    execution = await pending(secured)
    if already_approved:
        await approve(secured, execution)
    secured.now[0] += 300
    expired = await secured.approvals.expire(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert expired.state == ApprovalState.EXPIRED
    with pytest.raises(RuntimeFailure, match="expired"):
        await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED and result.error == "approval_expired"
    assert not secured.executed


async def test_reject_is_not_an_authorization_or_a_resumable_approval(secured: SecureKernel) -> None:
    execution = await pending(secured)
    rejected = await secured.approvals.reject(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert rejected.state == ApprovalState.REJECTED
    with pytest.raises(RuntimeFailure, match="replayed"):
        await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.FAILED and result.error == "approval_rejected"
    assert not secured.executed


async def test_approve_reject_and_expire_require_correct_user_execution_and_scope(
    secured: SecureKernel,
) -> None:
    execution = await pending(secured)
    for action in (secured.approvals.approve, secured.approvals.reject, secured.approvals.expire):
        with pytest.raises(SecurityDenied):
            await action(SCOPE, execution.id, execution.approval_request_id, user_id="another-user")
        with pytest.raises(NotFound):
            await action(SCOPE, "another-execution", execution.approval_request_id, user_id=SCOPE.user_id)
        with pytest.raises(NotFound):
            await action(
                CapabilityScope(workspace_id="other", user_id=SCOPE.user_id),
                execution.id,
                execution.approval_request_id,
                user_id=SCOPE.user_id,
            )
    with pytest.raises(RuntimeFailure, match="not due"):
        await secured.approvals.expire(
            SCOPE,
            execution.id,
            execution.approval_request_id,
            user_id=SCOPE.user_id,
        )


async def test_human_authorizer_is_mandatory_not_a_main_agent_self_assertion(secured: SecureKernel) -> None:
    execution = await pending(secured)
    no_human = ApprovalService(secured.approvals.repository, secured.invocations.security)
    with pytest.raises(SecurityDenied, match="human approval authorization"):
        await no_human.approve(
            SCOPE,
            execution.id,
            execution.approval_request_id,
            user_id=SCOPE.user_id,
        )


@pytest.mark.parametrize("when", ["before_approval", "after_approval", "after_start"])
@pytest.mark.parametrize("change", ["permissions", "policy"])
async def test_live_authorization_and_policy_are_rechecked(
    secured: SecureKernel,
    when: str,
    change: str,
) -> None:
    execution = await pending(secured)
    lease = None
    if when != "before_approval":
        await approve(secured, execution)
        await secured.engine.resume(SCOPE, execution.id)
    if when == "after_start":
        lease = await secured.engine.start(SCOPE, execution.id)
    update = {"user_grants": ()} if change == "permissions" else {"allowed_side_effects": ("none",)}
    secured.policies.value = secured.policies.value.model_copy(update=update)
    if when == "before_approval":
        with pytest.raises(SecurityDenied):
            await approve(secured, execution)
    elif when == "after_approval":
        with pytest.raises(SecurityDenied):
            await secured.engine.start(SCOPE, execution.id)
    else:
        assert lease is not None
        failed = await secured.engine.dispatch(lease)
        assert failed.state == ExecutionState.FAILED and failed.error == "security_denied"
    assert not secured.executed
    receipt = await secured.approvals.get(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert receipt.state != ApprovalState.CONSUMED


async def test_approval_cannot_be_replayed_across_dispatches_or_execution_ids(secured: SecureKernel) -> None:
    execution = await pending(secured)
    other = await pending(secured)
    await approve(secured, execution)
    with pytest.raises(NotFound):
        await secured.approvals.approve(
            SCOPE,
            other.id,
            execution.approval_request_id,
            user_id=SCOPE.user_id,
        )
    await secured.engine.resume(SCOPE, execution.id)
    lease = await secured.engine.start(SCOPE, execution.id)
    outcomes = await asyncio.gather(
        secured.engine.dispatch(lease),
        secured.engine.dispatch(lease),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RuntimeFailure) for item in outcomes) == 1
    assert secured.executed == [execution.id]


async def test_repeated_resume_without_approval_does_not_create_new_request(secured: SecureKernel) -> None:
    execution = await pending(secured)
    await secured.engine.resume(SCOPE, execution.id)
    repeated = await secured.engine.run(SCOPE, execution.id)
    assert repeated.state == ExecutionState.SUSPENDED
    assert repeated.approval_request_id == execution.approval_request_id
    assert repeated.usage.function_calls == 0


async def test_each_post_approval_attempt_needs_fresh_consumable_confirmation(
    secured: SecureKernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def suspend(context: ExecutorContext) -> ExecutorResult:
        secured.executed.append(context.execution.id)
        return ExecutorResult(output={"checkpoint": True}, suspended=True)

    secured.policies.value = secured.policies.value.model_copy(
        update={"preference": ApprovalPreference.ALWAYS}
    )
    monkeypatch.setitem(secured.functions._functions, "local_write", suspend)
    first = await pending(secured, "local_write")
    await approve(secured, first)
    await secured.engine.resume(SCOPE, first.id)
    suspended = await secured.engine.run(SCOPE, first.id)
    assert suspended.state == ExecutionState.SUSPENDED
    await secured.engine.resume(SCOPE, first.id)
    second = await secured.engine.run(SCOPE, first.id)
    assert (
        second.state == ExecutionState.SUSPENDED and second.approval_request_id != first.approval_request_id
    )
    assert secured.executed == [first.id]


async def test_sql_guards_prevent_binding_changes_and_consumption_replay(secured: SecureKernel) -> None:
    execution = await pending(secured)
    for assignment in (
        "execution_id='other'",
        "user_id='other'",
        "expires_at=999999",
        "binding_hash='changed'",
        "state='consumed'",
    ):
        async with secured.catalog.sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(f"UPDATE approval_request SET {assignment} WHERE id=:id"),
                    {"id": execution.approval_request_id},
                )
            await session.rollback()
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    await secured.engine.run(SCOPE, execution.id)
    async with secured.catalog.sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE approval_request SET state='approved' WHERE id=:id"),
                {"id": execution.approval_request_id},
            )
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 2


async def test_phase5_idempotency_fingerprint_remains_compatible(secured: SecureKernel) -> None:
    version = await capability(secured)
    original = request(version).model_copy(update={"request_key": "phase5-replay"})
    old = original.model_dump(mode="json")
    old["context"] = {
        field: old["context"][field]
        for field in ("scope", "actor_id", "source", "correlation_id", "permission_ceiling")
    }
    fingerprint = request_fingerprint("capability.invoke", old)
    created = await secured.invocations.invoke(original)
    async with secured.catalog.sessions() as session:
        from shovel.db.model.runtime import Operation

        row = await session.get(Operation, created.id)
        assert row is not None and row.request_fingerprint == fingerprint
    assert (await secured.invocations.invoke(original)).id == created.id


@pytest.mark.parametrize("boundary", ["proactive_boundary", "workflow_boundary", "skill_boundary"])
async def test_invocation_cannot_skip_a_narrowing_boundary(
    secured: SecureKernel,
    boundary: str,
) -> None:
    version = await capability(secured)
    with pytest.raises(SecurityDenied):
        await secured.invocations.invoke(request(version, **{boundary: (WRITE,)}))
    assert not secured.executed


async def test_pending_decision_is_atomic_under_concurrent_human_responses(secured: SecureKernel) -> None:
    execution = await pending(secured)
    results = await asyncio.gather(
        approve(secured, execution),
        approve(secured, execution),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RuntimeFailure) for item in results) == 1
    receipt = await secured.approvals.get(
        SCOPE,
        execution.id,
        execution.approval_request_id,
        user_id=SCOPE.user_id,
    )
    assert receipt.state == ApprovalState.APPROVED and receipt.consumed_at is None


async def test_gate_order_and_consumption_precede_the_executor(
    secured: SecureKernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await pending(secured)
    await approve(secured, execution)
    await secured.engine.resume(SCOPE, execution.id)
    lease = await secured.engine.start(SCOPE, execution.id)
    gate = secured.invocations.security
    order: list[str] = []
    authorization, risk, policy, approval = (
        gate.authorization.authorize,
        gate.risks.evaluate,
        gate.policies.evaluate,
        gate.approvals.resolve,
    )

    async def authorize(*args: object, **kwargs: object) -> object:
        order.append("authorization")
        return await authorization(*args, **kwargs)

    def evaluate_risk(*args: object, **kwargs: object) -> object:
        order.append("risk")
        return risk(*args, **kwargs)

    def evaluate_policy(*args: object, **kwargs: object) -> None:
        order.append("policy")
        policy(*args, **kwargs)

    def resolve_approval(*args: object, **kwargs: object) -> object:
        order.append("approval")
        return approval(*args, **kwargs)

    async def effect(_context: ExecutorContext) -> ExecutorResult:
        receipt = await secured.approvals.get(
            SCOPE,
            execution.id,
            execution.approval_request_id,
            user_id=SCOPE.user_id,
        )
        assert receipt.state == ApprovalState.CONSUMED
        order.append("execution")
        return ExecutorResult(output={"done": True})

    monkeypatch.setattr(gate.authorization, "authorize", authorize)
    monkeypatch.setattr(gate.risks, "evaluate", evaluate_risk)
    monkeypatch.setattr(gate.policies, "evaluate", evaluate_policy)
    monkeypatch.setattr(gate.approvals, "resolve", resolve_approval)
    monkeypatch.setitem(secured.functions._functions, "external_write", effect)
    assert (await secured.engine.dispatch(lease)).state == ExecutionState.SUCCEEDED
    assert order == ["authorization", "risk", "policy", "approval", "execution"]


async def test_policy_change_invalidates_an_approved_binding_instead_of_reusing_it(
    secured: SecureKernel,
) -> None:
    execution = await pending(secured)
    await approve(secured, execution)
    secured.policies.value = secured.policies.value.model_copy(update={"require_approval": True})
    await secured.engine.resume(SCOPE, execution.id)
    replaced = await secured.engine.run(SCOPE, execution.id)
    assert replaced.state == ExecutionState.SUSPENDED
    assert replaced.approval_request_id != execution.approval_request_id
    assert not secured.executed


async def test_version_eight_upgrade_preserves_execution_and_adds_approval_store(
    secured: SecureKernel,
) -> None:
    execution = await secured.invocations.invoke(request(await capability(secured)))
    async with secured.catalog.sessions() as session:
        await session.execute(text("DROP TABLE approval_request"))
        await session.execute(text("DELETE FROM schema_migration WHERE version=9"))
        await session.commit()
        connection = await session.connection()
        engine = connection.engine
    await upgrade_database(engine)
    await upgrade_database(engine)
    restored = await secured.repository.get(SCOPE, execution.id)
    assert restored == execution
    result = await secured.engine.run(SCOPE, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and secured.executed == [execution.id]
    async with secured.catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ApprovalRow)) == 0
        assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
