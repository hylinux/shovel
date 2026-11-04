from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityScope,
    FunctionContract,
    FunctionImplementation,
    SideEffectProfile,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.effect_repository import SQLiteEffectRepository
from shovel.db.execution_control_repository import SQLiteExecutionControlRepository
from shovel.db.model.effect import ExternalEffect, ExternalEffectEvent
from shovel.execution.control import ExecutionWakeupService, RecoveryService, TransientExecutionError
from shovel.execution.domain import ExecutionState
from shovel.execution.effects import EffectEvidence, EffectIntent, ExternalEffectService
from shovel.execution.ports import ExecutorContext
from shovel.identity.domain import AuthenticationState
from shovel.identity.waits import AuthenticationWaitRequired
from shovel.protection.domain import content_digest
from shovel.protection.egress import mark_enterprise
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import SecurityDenied
from shovel.security.services import ApprovalService, AuthorizationService, SecurityGate
from test_capability_registry import SCOPE
from test_capability_registry import catalog as catalog
from test_enterprise_identity_contracts import token_request
from test_execution_engine import Kernel, invocation, publish
from test_execution_engine import kernel as kernel
from test_security_core import ALL, READ, WRITE, Human, Policies

BODY = "EXTERNAL-BODY-SENTINEL"
SECRET = "EXTERNAL-CREDENTIAL-SENTINEL"


class FixtureAdapter:
    permission = WRITE

    def __init__(self) -> None:
        self.completion_output = {}
        self.calls = []
        self.applied = set()
        self.probes = 0
        self.binding = "binding-v1"
        self.error = None
        self.before_authorize = None
        self.before_send = None
        self.before_probe = None
        self.unknown = False

    async def prepare(self, execution):
        return EffectIntent(
            implementation_id="fixture.send",
            contract_digest=content_digest({"contract": "fixture-v1"}),
            target_digest=content_digest({"target": "fixture.invalid"}),
            request_digest=content_digest(execution.input),
            binding_digest=content_digest({"binding": self.binding}),
        )

    async def authorize(self, lease, intent):
        assert intent == await self.prepare(lease.execution)
        if self.before_authorize:
            await self.before_authorize()

    async def send(self, execution, intent, *, idempotency_key):
        self.calls.append(idempotency_key)
        self.applied.add(idempotency_key)
        if self.before_send:
            await self.before_send()
        if self.error:
            raise self.error(SECRET)
        return EffectEvidence(outcome="applied", evidence_digest=content_digest({"applied": idempotency_key}))

    async def authorize_reconciliation(self, execution, receipt):
        assert execution.id == receipt.execution_id
        if receipt.intent != await self.prepare(execution):
            raise SecurityDenied("Fixture reconciliation bindings changed.")

    async def reconcile(self, execution, receipt, *, idempotency_key):
        self.probes += 1
        if self.before_probe:
            await self.before_probe()
        return EffectEvidence(
            outcome="unknown"
            if self.unknown
            else ("applied" if idempotency_key in self.applied else "not_applied"),
            evidence_digest=content_digest({"probe": idempotency_key, "unknown": self.unknown}),
        )


@dataclass
class Effects:
    kernel: Kernel
    adapter: FixtureAdapter
    service: ExternalEffectService
    wakeups: ExecutionWakeupService
    recovery: RecoveryService
    policies: Policies

    async def ready(self):
        before = len(self.adapter.calls)
        content = CapabilityContent(
            implementation=FunctionImplementation(implementation_id="fixture.send"),
            required_permissions=(WRITE,),
            permission_boundary=ALL,
            side_effect_profile=SideEffectProfile(kind="external_write"),
        )
        version = await publish(self.kernel.catalog, content)
        request = invocation(version).model_copy(
            update={
                "input": {"body": BODY},
                "context": invocation(version).context.model_copy(update={"permission_ceiling": ALL}),
            }
        )
        queued = await self.kernel.invocations.invoke(request)
        waiting = await self.kernel.engine.run(SCOPE, queued.id)
        assert waiting.state == ExecutionState.SUSPENDED
        assert waiting.approval_request_id and len(self.adapter.calls) == before
        await self.approve(waiting)
        return await self.kernel.engine.resume(SCOPE, queued.id)

    async def approve(self, waiting):
        approvals = ApprovalService(
            SQLiteApprovalRepository(self.kernel.catalog.sessions, clock=lambda: self.kernel.now[0]),
            self.kernel.invocations.security,
            authorizer=Human(),
        )
        await approvals.approve(SCOPE, waiting.id, waiting.approval_request_id, user_id=SCOPE.user_id)

    async def finish(self, identifier):
        result = await self.kernel.engine.run(SCOPE, identifier)
        if result.approval_request_id and result.error == "approval_pending":
            await self.approve(result)
            await self.kernel.engine.resume(SCOPE, identifier)
            result = await self.kernel.engine.run(SCOPE, identifier)
        return result

    async def facts(self, identifier):
        return (await self.service.repository.inspect(SCOPE, identifier))[1]


@pytest.fixture
def effects(kernel: Kernel) -> Effects:
    registry = CapabilityRegistry(
        kernel.catalog.repository,
        functions=(
            FunctionContract(implementation_id="demo.read"),
            FunctionContract(
                implementation_id="fixture.send",
                required_permissions=(WRITE,),
                side_effect_profile=SideEffectProfile(kind="external_write"),
                retry_safe=True,
            ),
        ),
    )
    kernel.catalog.registry = kernel.invocations.registry = registry
    kernel.catalog.service = CapabilityService(registry, authorizer=kernel.catalog.publisher)
    policies = Policies()
    kernel.invocations.security = SecurityGate(AuthorizationService(policies))

    def clock():
        return kernel.now[0]

    journal = SQLiteEffectRepository(kernel.catalog.sessions, clock=clock)
    adapter = FixtureAdapter()
    service = ExternalEffectService(journal, kernel.invocations.security, {"fixture.send": adapter})
    controls = SQLiteExecutionControlRepository(kernel.catalog.sessions, clock=clock)
    wakeups = ExecutionWakeupService(controls)
    kernel.engine.effects, kernel.engine.wakeups = service, wakeups
    return Effects(kernel, adapter, service, wakeups, RecoveryService(controls, lambda _: True), policies)


async def test_owned_dispatch_receipt_contains_only_metadata(effects: Effects):
    queued = await effects.ready()
    completed = await effects.kernel.engine.run(SCOPE, queued.id)
    assert completed.state == ExecutionState.SUCCEEDED and completed.output == {}
    (receipt,) = await effects.facts(queued.id)
    assert receipt.state == "applied" and not receipt.resume_required
    assert len(effects.adapter.calls) == 1
    async with effects.kernel.catalog.sessions() as session:
        events = list(
            await session.scalars(select(ExternalEffectEvent).order_by(ExternalEffectEvent.revision))
        )
        assert [event.state for event in events] == ["intent", "dispatched", "applied"]
        rows = (await session.execute(text("SELECT * FROM external_effect"))).mappings().all()
        audit = (await session.execute(text("SELECT * FROM external_effect_event"))).mappings().all()
        encoded = json.dumps([dict(row) for row in [*rows, *audit]])
        assert BODY not in encoded and SECRET not in encoded


@pytest.mark.parametrize("error", [TimeoutError, TransientExecutionError, RuntimeFailure])
async def test_wire_failure_never_retries_or_resumes_even_if_marked_safe(effects: Effects, error):
    queued = await effects.ready()
    effects.adapter.error = error
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.state == ExecutionState.SUSPENDED
    assert result.wait_reason == "waiting_for_reconciliation"
    assert SECRET not in result.model_dump_json()
    assert (await effects.facts(queued.id))[0].state == "uncertain"
    with pytest.raises(RuntimeFailure, match="reconciliation"):
        await effects.kernel.engine.resume(SCOPE, queued.id)
    with pytest.raises(RuntimeFailure, match="reconciliation"):
        await effects.wakeups.schedule_resume(SCOPE, queued.id, wake_at=effects.kernel.now[0])
    await effects.wakeups.run_due()
    assert await effects.recovery.recover() == 0
    assert len(effects.adapter.calls) == 1
    assert not await effects.kernel.engine.run_once()


@pytest.mark.parametrize("phase", ["intent", "dispatched", "applied"])
async def test_crash_windows_reconcile_then_explicit_resume_without_duplicate(effects: Effects, phase):
    queued = await effects.ready()
    kernel = effects.kernel
    lease = await kernel.engine.start(SCOPE, queued.id)
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    intent = await effects.adapter.prepare(lease.execution)
    receipt, key = await effects.service.repository.prepare(lease, intent, kernel.invocations.security)
    if phase != "intent":
        await effects.service.repository.dispatch(lease, receipt.id, kernel.invocations.security)
        evidence = await effects.adapter.send(lease.execution, intent, idempotency_key=key)
        if phase == "applied":
            await effects.service.repository.received(lease, receipt.id, evidence)
    assert await effects.recovery.recover() == 1
    with pytest.raises(RuntimeFailure, match="Stale"):
        await kernel.repository.check_active(lease, kernel.invocations.security)
    if phase != "applied":
        with pytest.raises(RuntimeFailure, match="Reconcile"):
            await effects.service.resume(SCOPE, queued.id)
    await effects.service.reconcile(SCOPE, queued.id)
    assert effects.adapter.probes == (0 if phase == "applied" else 1)
    with pytest.raises(RuntimeFailure, match="explicit resume"):
        await kernel.engine.resume(SCOPE, queued.id)
    await effects.wakeups.run_due()
    assert not await kernel.engine.run_once()
    await effects.service.resume(SCOPE, queued.id)
    assert (await effects.finish(queued.id)).state == ExecutionState.SUCCEEDED
    assert effects.adapter.calls == [key]
    assert (await effects.facts(queued.id))[0].attempt >= receipt.attempt


async def test_unknown_reconciliation_is_not_proof_of_no_effect(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)
    effects.adapter.unknown = True
    await effects.service.reconcile(SCOPE, queued.id)
    assert (await effects.facts(queued.id))[0].state == "uncertain"
    with pytest.raises(RuntimeFailure, match="Reconcile"):
        await effects.service.resume(SCOPE, queued.id)
    assert len(effects.adapter.calls) == 1


@pytest.mark.parametrize("change", ["permission", "binding", "approval"])
async def test_fresh_pre_send_revalidation_proves_not_sent(effects: Effects, change):
    queued = await effects.ready()

    async def revoke():
        if change == "permission":
            effects.policies.value = effects.policies.value.model_copy(update={"user_grants": (READ,)})
        elif change == "approval":
            effects.policies.value = effects.policies.value.model_copy(update={"require_approval": True})
        else:
            effects.adapter.binding = "changed"

    effects.adapter.before_authorize = revoke
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.state == ExecutionState.FAILED and result.error == "security_denied"
    assert effects.adapter.calls == []
    assert (await effects.facts(queued.id))[0].state == "not_sent"


async def test_changed_bindings_and_wrong_scope_cannot_reconcile(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)
    effects.adapter.binding = "new-target-or-policy"
    with pytest.raises(SecurityDenied, match="original"):
        await effects.service.reconcile(SCOPE, queued.id)
    wrong = CapabilityScope(workspace_id="other", user_id=SCOPE.user_id)
    with pytest.raises(NotFound):
        await effects.service.reconcile(wrong, queued.id)
    assert effects.adapter.probes == 0 and len(effects.adapter.calls) == 1


async def test_same_step_changed_arguments_conflict_and_concurrent_intents_dedupe(effects: Effects):
    queued = await effects.ready()
    kernel = effects.kernel
    lease = await kernel.engine.start(SCOPE, queued.id)
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    intent = await effects.adapter.prepare(lease.execution)
    results = await asyncio.gather(
        effects.service.repository.prepare(lease, intent, kernel.invocations.security),
        effects.service.repository.prepare(lease, intent, kernel.invocations.security),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RuntimeFailure) for result in results) == 1
    changed = intent.model_copy(update={"request_digest": "0" * 64})
    with pytest.raises(RuntimeFailure, match="changed"):
        await effects.service.repository.prepare(lease, changed, kernel.invocations.security)
    assert len(await effects.facts(queued.id)) == 1 and not effects.adapter.calls


async def test_cancelled_execution_keeps_late_receipt_without_resurrection(effects: Effects):
    queued = await effects.ready()
    entered = asyncio.Event()

    async def wait_for_cancel():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    effects.adapter.before_send = wait_for_cancel
    running = asyncio.create_task(effects.kernel.engine.run(SCOPE, queued.id))
    await asyncio.wait_for(entered.wait(), 2)
    await effects.kernel.engine.cancel(SCOPE, queued.id)
    result = await asyncio.wait_for(running, 2)
    assert result.state == ExecutionState.CANCELLED
    (receipt,) = await effects.facts(queued.id)
    assert receipt.state == "applied" and receipt.resume_required
    await effects.service.reconcile(SCOPE, queued.id)
    with pytest.raises(RuntimeFailure):
        await effects.service.resume(SCOPE, queued.id)
    assert len(effects.adapter.calls) == 1


async def test_reconciliation_claim_excludes_concurrency_and_expires(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)
    (receipt,) = await effects.facts(queued.id)
    token, _ = await effects.service.repository.claim_reconciliation(SCOPE, queued.id, receipt)
    with pytest.raises(RuntimeFailure, match="already active"):
        await effects.service.reconcile(SCOPE, queued.id)
    effects.kernel.now[0] += 61
    evidence = EffectEvidence(outcome="not_applied", evidence_digest="0" * 64)
    with pytest.raises(RuntimeFailure, match="stale"):
        await effects.service.repository.reconciled(SCOPE, queued.id, receipt.id, token, evidence)
    await effects.service.reconcile(SCOPE, queued.id)
    assert (await effects.facts(queued.id))[0].state == "applied"


async def test_original_deadline_and_receipt_immutability(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)
    await effects.service.reconcile(SCOPE, queued.id)
    current = await effects.kernel.repository.get(SCOPE, queued.id)
    effects.kernel.now[0] = current.deadline_at
    with pytest.raises(RuntimeFailure, match="deadline"):
        await effects.service.resume(SCOPE, queued.id)
    async with effects.kernel.catalog.sessions() as session:
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(text("UPDATE external_effect SET fingerprint='changed'"))
        await session.rollback()
        with pytest.raises(IntegrityError, match="retained"):
            await session.execute(text("DELETE FROM external_effect"))
        await session.rollback()
        with pytest.raises(IntegrityError, match="immutable"):
            await session.execute(text("UPDATE external_effect_event SET state='not_applied'"))
        await session.rollback()
        assert len(list(await session.scalars(select(ExternalEffect)))) == 1


async def test_authentication_challenge_after_wire_is_not_an_authentication_wait(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = lambda _: AuthenticationWaitRequired(
        token_request(), AuthenticationState.CLAIMS_CHALLENGE_REQUIRED, "0" * 64
    )
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.wait_reason == "waiting_for_reconciliation"
    assert len(effects.adapter.calls) == 1
    async with effects.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM authentication_request")) == 0


async def test_retry_controller_cannot_override_a_durable_intent(effects: Effects):
    queued = await effects.ready()
    kernel = effects.kernel
    lease = await kernel.engine.start(SCOPE, queued.id)
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    await effects.service.repository.prepare(
        lease, await effects.adapter.prepare(lease.execution), kernel.invocations.security
    )
    result = await effects.wakeups.retry(lease, safe=True)
    assert result.state == ExecutionState.SUSPENDED
    assert result.wait_reason == "waiting_for_reconciliation"
    await effects.wakeups.run_due()
    assert not await kernel.engine.run_once()
    assert not effects.adapter.calls


async def test_expired_execution_keeps_applied_receipt_but_does_not_report_success(effects: Effects):
    queued = await effects.ready()

    async def expire():
        current = await effects.kernel.repository.get(SCOPE, queued.id)
        effects.kernel.now[0] = current.deadline_at + 1

    effects.adapter.before_send = expire
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.state == ExecutionState.FAILED and result.error == "budget_exceeded"
    assert (await effects.facts(queued.id))[0].state == "applied"
    assert len(effects.adapter.calls) == 1


async def test_failed_probe_retains_unknown_outcome_without_leaking_provider_errors(effects: Effects):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)

    async def fail():
        raise RuntimeError(SECRET)

    effects.adapter.before_probe = fail
    with pytest.raises(RuntimeFailure, match="remains unresolved") as error:
        await effects.service.reconcile(SCOPE, queued.id)
    assert SECRET not in str(error.value)
    assert (await effects.facts(queued.id))[0].state == "uncertain"


async def test_new_execution_or_request_key_cannot_bypass_an_uncertain_effect(effects: Effects):
    original = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, original.id)
    other = await effects.ready()
    result = await effects.kernel.engine.run(SCOPE, other.id)
    assert result.state == ExecutionState.FAILED and result.error == "security_denied"
    assert len(effects.adapter.calls) == 1
    assert (await effects.facts(original.id))[0].state == "uncertain"


async def test_protected_flow_and_uninstalled_adapter_remain_fail_closed(effects: Effects):
    queued = await effects.ready()

    async def protect():
        mark_enterprise()

    effects.adapter.before_authorize = protect
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.error == "data_use_denied" and effects.adapter.calls == []
    assert (await effects.facts(queued.id))[0].state == "not_sent"
    other = await effects.ready()
    effects.service._adapters.clear()
    result = await effects.kernel.engine.run(SCOPE, other.id)
    assert result.error == "security_denied" and effects.adapter.calls == []


async def test_ambiguous_dispatch_commit_is_not_proof_of_no_send(effects: Effects, monkeypatch):
    queued = await effects.ready()
    dispatch = effects.service.repository.dispatch

    async def ambiguous(*arguments):
        await dispatch(*arguments)
        raise RuntimeFailure(SECRET)

    monkeypatch.setattr(effects.service.repository, "dispatch", ambiguous)
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.wait_reason == "waiting_for_reconciliation" and effects.adapter.calls == []
    assert (await effects.facts(queued.id))[0].state == "uncertain"


async def test_lost_receipt_commit_reconciles_without_resending(effects: Effects, monkeypatch):
    queued = await effects.ready()

    async def lose(*arguments):
        raise RuntimeFailure(SECRET)

    monkeypatch.setattr(effects.service.repository, "received", lose)
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.wait_reason == "waiting_for_reconciliation"
    await effects.service.reconcile(SCOPE, queued.id)
    await effects.service.resume(SCOPE, queued.id)
    result = await effects.finish(queued.id)
    assert result.state == ExecutionState.SUCCEEDED and len(effects.adapter.calls) == 1


async def test_a_receipt_without_a_durable_wire_attempt_is_rejected(effects: Effects):
    queued = await effects.ready()
    kernel = effects.kernel
    lease = await kernel.engine.start(SCOPE, queued.id)
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    receipt, _ = await effects.service.repository.prepare(
        lease, await effects.adapter.prepare(lease.execution), kernel.invocations.security
    )
    async with kernel.catalog.sessions() as session:
        with pytest.raises(IntegrityError, match="replaced"):
            await session.execute(
                text("INSERT OR REPLACE INTO external_effect SELECT * FROM external_effect")
            )
        await session.rollback()
    with pytest.raises(RuntimeFailure, match="matching wire attempt"):
        await effects.service.repository.received(
            lease, receipt.id, EffectEvidence(outcome="applied", evidence_digest="0" * 64)
        )
    assert (await effects.facts(queued.id))[0].state == "intent"


async def test_invalid_static_receipt_is_rejected_before_any_send(effects: Effects):
    queued = await effects.ready()
    effects.adapter.completion_output = {"access_token": SECRET}
    result = await effects.kernel.engine.run(SCOPE, queued.id)
    assert result.state == ExecutionState.FAILED
    assert not effects.adapter.calls and not await effects.facts(queued.id)
    assert SECRET not in result.model_dump_json()


async def test_conflicting_receipts_remain_quarantined_even_after_another_applied_receipt(effects: Effects):
    queued = await effects.ready()
    kernel = effects.kernel
    lease = await kernel.engine.start(SCOPE, queued.id)
    await kernel.repository.dispatch(lease, kernel.invocations.security)
    receipt, key = await effects.service.repository.prepare(
        lease, await effects.adapter.prepare(lease.execution), kernel.invocations.security
    )
    await effects.service.repository.dispatch(lease, receipt.id, kernel.invocations.security)
    for outcome in ("applied", "not_applied", "applied"):
        await effects.service.repository.received(
            lease,
            receipt.id,
            EffectEvidence(outcome=outcome, evidence_digest=content_digest({"outcome": outcome})),
        )
    (current,) = await effects.facts(queued.id)
    assert current.conflicted and current.state == "uncertain"
    with pytest.raises(RuntimeFailure, match="not settled"):
        await kernel.engine.succeed(lease, {})
    assert await effects.recovery.recover() == 1
    effects.adapter.applied.add(key)
    await effects.service.reconcile(SCOPE, queued.id)
    (current,) = await effects.facts(queued.id)
    assert current.conflicted and current.state == "uncertain"
    with pytest.raises(RuntimeFailure, match="Reconcile"):
        await effects.service.resume(SCOPE, queued.id)


@pytest.mark.parametrize("change", ["permission", "binding"])
async def test_reconciliation_rechecks_authority_before_accepting_evidence(effects: Effects, change):
    queued = await effects.ready()
    effects.adapter.error = TimeoutError
    await effects.kernel.engine.run(SCOPE, queued.id)

    async def revoke():
        if change == "permission":
            effects.policies.value = effects.policies.value.model_copy(update={"user_grants": (READ,)})
        else:
            effects.adapter.binding = "changed-during-reconciliation"

    effects.adapter.before_probe = revoke
    with pytest.raises(SecurityDenied):
        await effects.service.reconcile(SCOPE, queued.id)
    assert (await effects.facts(queued.id))[0].state == "uncertain"
    assert len(effects.adapter.calls) == 1


async def test_another_execution_context_cannot_borrow_the_active_lease(effects: Effects):
    queued = await effects.ready()
    lease = await effects.kernel.engine.start(SCOPE, queued.id)
    context = ExecutorContext(
        execution=lease.execution.model_copy(update={"id": "another-execution"}),
        consume=AsyncMock(),
        call=AsyncMock(),
        lease=lease,
    )
    with pytest.raises(SecurityDenied, match="exact owned"):
        await effects.service.execute(context)
    assert not effects.adapter.calls and not await effects.facts(queued.id)


async def test_effect_facts_use_full_sync_before_wire_and_after_receipt(effects: Effects):
    engine = effects.kernel.catalog.sessions.kw["bind"]
    durability = []

    def observe(connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(("INSERT INTO external_effect", "UPDATE external_effect")):
            durability.append(connection.exec_driver_sql("PRAGMA synchronous").scalar_one())

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        queued = await effects.ready()

        async def before_wire():
            assert (await effects.facts(queued.id))[0].state == "dispatched"
            assert durability and all(value == 2 for value in durability)

        effects.adapter.before_send = before_wire
        assert (await effects.kernel.engine.run(SCOPE, queued.id)).state == ExecutionState.SUCCEEDED
        assert (await effects.facts(queued.id))[0].state == "applied"
        assert durability and all(value == 2 for value in durability)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
