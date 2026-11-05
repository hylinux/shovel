from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shovel.enterprise.acceptance import (
    LIVE_CHECKS,
    CanaryUse,
    LiveVerificationRequest,
    RolloutReviewer,
)
from shovel.enterprise.rollout import (
    LiveMeasurement,
    LiveReport,
    RolloutApproval,
    RolloutGate,
    RolloutMaterial,
    RolloutState,
    SignedLiveAcceptanceAuthority,
)
from shovel.protection.domain import DataAction, DataDestination, ProtectionDeniedError, content_digest
from test_enterprise_acceptance import evidence, plan
from test_organization_security import catalog as catalog
from test_organization_security import kernel as kernel
from test_organization_security import metadata_flow as metadata_flow
from test_organization_security import signer as signer


def make_rollout(signer, *, proposal=None, clock=lambda: 1000):
    from test_organization_security import signer as signer_fixture

    independent = signer_fixture.__wrapped__()
    proposal = proposal or plan()
    receipts = evidence()
    report = LiveReport(
        purpose="shovel.enterprise.live-report.v1",
        organization_id="organization",
        revision=1,
        plan_digest=content_digest(proposal.model_dump(mode="json")),
        evidence_digests=tuple(
            content_digest(item.model_dump(mode="json"))
            for item in sorted(receipts, key=lambda item: item.lane)
        ),
        observed_at=900,
        expires_at=1500,
        measurements=tuple(
            LiveMeasurement(
                check=check, environment="installed_live", result="passed", receipt_digest="3" * 64
            )
            for check in LIVE_CHECKS
        ),
    )
    report_anchor, report_bytes = independent(report)
    approval = RolloutApproval(
        purpose="shovel.enterprise.rollout-approval.v1",
        organization_id="organization",
        revision=1,
        issued_at=950,
        expires_at=1400,
        plan=proposal,
        evidence=receipts,
        report_digest=report_anchor.envelope_sha256,
    )
    approval_anchor, approval_bytes = signer(approval)
    state = RolloutState(
        schema=1,
        organizationId="organization",
        approvalSigner=approval_anchor.certificate_sha256,
        verifierSigner=report_anchor.certificate_sha256,
        generation=3,
        highestRevision=1,
        status="enabled",
        approvalDigest=approval_anchor.envelope_sha256,
        reportDigest=report_anchor.envelope_sha256,
        enabledAt=980,
    )
    holder = [RolloutMaterial(state, approval_bytes, report_bytes, proposal.installation_digest)]
    authority = SignedLiveAcceptanceAuthority(lambda: holder[0], clock=clock)
    return SimpleNamespace(
        gate=RolloutGate(authority, nullcontext),
        authority=authority,
        holder=holder,
        proposal=proposal,
        report=report,
        approval=approval,
        signer=signer,
        independent=independent,
    )


def gate_args(proposal):
    return dict(
        scope=proposal.scope,
        organization_id="organization",
        connection_id=proposal.connection_id,
        organization_policy_digest=proposal.organization_policy_digest,
        governance_policy_digest=proposal.governance_policy_digest,
        provider_manifest_digest=proposal.provider_manifest_digest,
        connection_binding_digest=proposal.connection_binding_digest,
        execution_created_at=1000,
    )


def test_real_signature_verifier_qualifies_review_but_only_enabled_gate_allows(signer):
    harness = make_rollout(signer)
    review = RolloutReviewer(harness.authority, clock=lambda: 1000)
    assert review.review(harness.proposal, evidence()).status == "eligible_for_operator_review"
    assert harness.gate.require(**gate_args(harness.proposal))
    for status in ("approved", "paused", "revoked"):
        harness.holder[0] = replace(
            harness.holder[0], state=harness.holder[0].state.model_copy(update={"status": status})
        )
        with pytest.raises(ProtectionDeniedError):
            harness.gate.require(**gate_args(harness.proposal))
    with pytest.raises(ProtectionDeniedError):
        RolloutGate().require(**gate_args(harness.proposal))


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "pin",
        "installation",
        "report",
        "plan",
        "offline",
        "missing",
        "failed",
        "future",
        "expired",
    ],
)
def test_signed_live_verifier_rejects_forged_incomplete_and_wrong_deployment_evidence(signer, change):
    harness = make_rollout(signer)
    material = harness.holder[0]
    if change == "signature":
        material = replace(material, report=material.report + b" ")
    elif change == "pin":
        material = replace(material, state=material.state.model_copy(update={"verifier_signer": "0" * 64}))
    elif change == "installation":
        material = replace(material, installation_digest="0" * 64)
    else:
        report, approval = harness.report, harness.approval
        if change == "report":
            report = report.model_copy(update={"evidence_digests": ("0" * 64,)})
        elif change == "plan":
            report = report.model_copy(update={"plan_digest": "0" * 64})
        elif change == "offline":
            approval = approval.model_copy(
                update={
                    "evidence": (
                        evidence()[0].model_copy(update={"source_tree_clean": False}),
                        *evidence()[1:],
                    )
                }
            )
        elif change == "missing":
            report = report.model_copy(update={"measurements": report.measurements[:-1]})
        elif change == "failed":
            report = report.model_copy(
                update={
                    "measurements": (
                        report.measurements[0].model_copy(update={"result": "failed"}),
                        *report.measurements[1:],
                    )
                }
            )
        elif change == "future":
            report = report.model_copy(update={"observed_at": 1001})
        else:
            approval = approval.model_copy(update={"expires_at": 1000})
        report_anchor, report_bytes = harness.independent(report)
        approval = approval.model_copy(update={"report_digest": report_anchor.envelope_sha256})
        approval_anchor, approval_bytes = signer(approval)
        material = replace(
            material,
            report=report_bytes,
            approval=approval_bytes,
            state=material.state.model_copy(
                update={
                    "report_digest": report_anchor.envelope_sha256,
                    "approval_digest": approval_anchor.envelope_sha256,
                }
            ),
        )
    harness.holder[0] = material
    with pytest.raises(ProtectionDeniedError):
        harness.gate.require(**gate_args(harness.proposal))


def test_nonce_and_exact_approval_evidence_bind_verification_and_old_generation_never_revives(signer):
    harness = make_rollout(signer)
    args = gate_args(harness.proposal)
    original = harness.gate.require(**args)
    request = LiveVerificationRequest(
        nonce="0" * 64,
        request_digest="0" * 64,
        plan=harness.proposal,
        evidence_digests=harness.report.evidence_digests,
        issued_at=1000,
    )
    from shovel.enterprise.acceptance import AcceptanceUnavailableError

    with pytest.raises(AcceptanceUnavailableError):
        harness.authority.verify(request)
    for status in ("paused", "enabled"):
        harness.holder[0] = replace(
            harness.holder[0],
            state=harness.holder[0].state.model_copy(
                update={
                    "status": status,
                    "generation": harness.holder[0].state.generation + 1,
                }
            ),
        )
    # Also holds in a freshly constructed Runtime after restart.
    restarted = RolloutGate(
        SignedLiveAcceptanceAuthority(lambda: harness.holder[0], clock=lambda: 1000), nullcontext
    )
    with pytest.raises(ProtectionDeniedError):
        restarted.require(**args, expected=original)
    assert restarted.require(**args) != original
    with pytest.raises(ProtectionDeniedError):
        restarted.require(**args, action=DataAction.SEND_TO_MODEL, destination=DataDestination.EXTERNAL_MODEL)
    with pytest.raises(ProtectionDeniedError):
        restarted.require(**{**args, "connection_binding_digest": "0" * 64})
    with pytest.raises(ProtectionDeniedError):
        restarted.require(**{**args, "organization_id": "another-organization"})
    with pytest.raises(ProtectionDeniedError):
        restarted.require(**args, action=DataAction.DISPLAY, destination=DataDestination.USER_SCREEN)


@pytest.mark.parametrize("change", ["control", "expiry"])
def test_control_change_during_signature_verification_cannot_issue_a_permit(signer, change):
    now = [1000]
    harness = make_rollout(signer, clock=lambda: now[0])
    original = harness.holder[0]
    changed = replace(original, state=original.state.model_copy(update={"generation": 4, "status": "paused"}))
    reads = 0

    def load():
        nonlocal reads
        reads += 1
        if reads == 1:
            return original
        if change == "expiry":
            now[0] = 1400
            return original
        return changed

    harness.authority.loader = load
    with pytest.raises(ProtectionDeniedError):
        harness.gate.require(**gate_args(harness.proposal))


def install_rollout(service, signer):
    current = service._trusted()
    connection = current.policy.connections[0]
    proposal = plan().model_copy(
        update={
            "scope": current.policy.scope,
            "connection_id": connection.profile.id,
            "permissions": (
                type(plan().permissions[0])(action="enterprise.read", resource=connection.profile.id),
            ),
            "organization_policy_digest": current.digest,
            "governance_policy_digest": service.protection.governance.policy()[1],
            "provider_manifest_digest": current.policy.manifest.content_hash,
            "connection_binding_digest": service.connection_digest(current, connection),
            "data_uses": (
                CanaryUse(action=DataAction.READ, destination=DataDestination.WORKFLOW_STATE),
                CanaryUse(action=DataAction.DISPLAY, destination=DataDestination.USER_SCREEN),
            ),
        }
    )
    harness = make_rollout(signer, proposal=proposal, clock=service.clock)
    service.rollout = harness.gate
    service.engine.invocations.admission = service.admit
    return harness


@pytest.mark.parametrize("change", ["pause", "revoke", "reenable", "expired", "missing"])
async def test_runtime_gate_blocks_old_admission_before_credentials_and_io(metadata_flow, change):
    flow = metadata_flow
    execution = await flow.invoke()
    harness = flow.rollout
    if change == "missing":
        flow.service.rollout = RolloutGate()
    elif change == "expired":
        flow.kernel.now[0] = 1400
    else:
        status = {"pause": "paused", "revoke": "revoked", "reenable": "enabled"}[change]
        harness.holder[0] = replace(
            harness.holder[0],
            state=harness.holder[0].state.model_copy(update={"status": status, "generation": 5}),
        )
    from shovel.security.domain import SecurityDenied

    with pytest.raises(SecurityDenied):
        await flow.kernel.engine.run(execution.snapshot.context.scope, execution.id)
    assert flow.authentication.calls == flow.remote.calls == 0
    assert flow.frames == []


async def test_enabled_rollout_neither_supplies_permissions_nor_admits_arbitrary_function(metadata_flow):
    flow = metadata_flow
    execution = await flow.invoke()
    from shovel.capability.domain import FunctionImplementation
    from shovel.security.domain import SecurityDenied

    snapshot = execution.snapshot
    with pytest.raises(SecurityDenied):
        flow.service.authorize_connection(
            snapshot.capability.model_copy(
                update={
                    "content": snapshot.capability.content.model_copy(
                        update={"implementation": FunctionImplementation(implementation_id="untrusted.query")}
                    )
                }
            ),
            snapshot.context,
            snapshot.granted_permissions,
        )
    flow.kernel.invocations.security.authorization.provider.enabled = False
    with pytest.raises(SecurityDenied):
        await flow.kernel.engine.run(snapshot.context.scope, execution.id)
    assert flow.authentication.calls == flow.remote.calls == 0
    assert flow.frames == []


async def test_runtime_pause_during_source_request_discards_response_and_closes_credential(
    metadata_flow, monkeypatch
):
    flow = metadata_flow
    original = flow.service.client.observe
    credentials = []

    async def observe(*args):
        credentials.append(args[-1])
        result = await original(*args)
        material = flow.rollout.holder[0]
        flow.rollout.holder[0] = replace(
            material, state=material.state.model_copy(update={"status": "paused", "generation": 4})
        )
        return result

    monkeypatch.setattr(flow.service.client, "observe", observe)
    execution = await flow.invoke()
    await flow.kernel.engine.run(flow.service._accepted.policy.scope, execution.id)
    assert flow.remote.calls == 1 and flow.frames == []
    with pytest.raises(ValueError):
        credentials[0].validate(flow.document.connections[0].authentication, now=1000)


async def test_runtime_revocation_after_durable_audit_denies_final_release(metadata_flow, monkeypatch):
    flow = metadata_flow
    original = flow.service.protection._transaction
    armed = False

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transaction():
        nonlocal armed
        async with original() as session:
            yield session
        if not armed:
            armed = True
            material = flow.rollout.holder[0]
            flow.rollout.holder[0] = replace(
                material, state=material.state.model_copy(update={"status": "revoked", "generation": 4})
            )

    # Arm only once source observations have completed; this transaction is the release audit.
    original_use = flow.service.protection.use_bound

    async def use_bound(*args, **kwargs):
        monkeypatch.setattr(flow.service.protection, "_transaction", transaction)
        return await original_use(*args, **kwargs)

    monkeypatch.setattr(flow.service.protection, "use_bound", use_bound)
    execution = await flow.invoke()
    await flow.kernel.engine.run(flow.service._accepted.policy.scope, execution.id)
    assert armed and flow.frames == []


async def test_frozen_rollout_survives_reload_and_result_consumer_holds_control_lease(
    metadata_flow, monkeypatch
):
    flow = metadata_flow
    execution = await flow.invoke()
    stored = await flow.kernel.engine.repository.get(execution.snapshot.context.scope, execution.id)
    assert stored.snapshot.enterprise_rollout_binding == execution.snapshot.enterprise_rollout_binding
    held = []

    from contextlib import contextmanager

    @contextmanager
    def lease():
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    flow.service.rollout.lease = lease
    original = flow.service.release_guard

    @contextmanager
    def guard(binding):
        with original(binding):
            assert held
            yield
            assert held

    monkeypatch.setattr(flow.service.protection, "release_guard", guard)
    from shovel.execution.domain import ExecutionState

    result = await flow.kernel.engine.run(execution.snapshot.context.scope, execution.id)
    assert result.state == ExecutionState.SUCCEEDED and flow.frames
    assert not held
