import copy
import json
import pickle

import pytest
from pydantic import ValidationError

from shovel.enterprise.domain import ConnectionBinding
from shovel.protection.domain import (
    ArtifactRef,
    AuthorizationSnapshot,
    DataAction,
    DataDestination,
    DataUseBinding,
    DependencyKind,
    Lineage,
    LineageEdge,
    ModelBinding,
    ModelProfile,
    ObligationKind,
    ObligationProof,
    PolicyDecision,
    PolicyObligation,
    PolicyOutcome,
    PolicyPermit,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionContext,
    ProtectionDeniedError,
    ReferenceUnavailableError,
    SourceBinding,
    SourceContentRef,
    TransientContentHandle,
    content_digest,
)
from test_enterprise_connection_contracts import resource
from test_enterprise_identity_contracts import DIGEST, OTHER_TENANT, TENANT, identity_binding


class TestAuthority:
    __test__ = False

    def __init__(self) -> None:
        self.revoked = False
        self.proof_rejected = False
        self.calls = 0

    def validate_decision(self, decision: PolicyDecision) -> None:
        self.calls += 1
        if self.revoked:
            raise ProtectionDeniedError("Decision is revoked.")

    def validate_proof(self, proof: ObligationProof, decision: PolicyDecision) -> None:
        if self.proof_rejected:
            raise ProtectionDeniedError("Evidence is not authentic.")


def source(**changes: object) -> SourceBinding:
    return SourceBinding.model_validate(
        {
            "resource": resource(),
            "identity_profile_id": "work",
            "principal_ref": "principal",
            "source_generation": 0,
            **changes,
        }
    )


def context(**changes: object) -> ProtectionContext:
    return ProtectionContext.model_validate(
        {
            "id": "context",
            "sources": (source(),),
            "classification_id": "internal",
            "protection_status": "unprotected",
            "encrypted": False,
            "rights": ("read",),
            "policy_source_ref": "organization-policy",
            "policy_version": DIGEST,
            "dlp_applicable": False,
            **changes,
        }
    )


def connection_binding() -> ConnectionBinding:
    return ConnectionBinding(
        connection_profile_id="connection",
        connection_revision=1,
        provider_id="provider",
        provider_version="1.0",
        manifest_digest=DIGEST,
        identity_profile_id="work",
        tenant_id=TENANT,
        endpoint="https://cluster.example",
    )


def binding(**changes: object) -> DataUseBinding:
    return DataUseBinding.model_validate(
        {
            "execution_id": "execution",
            "step_id": "step",
            "actor_id": "agent",
            "identities": (identity_binding(),),
            "connections": (connection_binding(),),
            "sources": (source(),),
            "protection_context_ref": "context",
            "protection_context_digest": context().content_hash,
            "content_digest": content_digest({"body": "CONTENT-SENTINEL"}),
            "action": DataAction.READ,
            "destination": DataDestination.USER_SCREEN,
            "policy_version": DIGEST,
            **changes,
        }
    )


def decision(**changes: object) -> PolicyDecision:
    return PolicyDecision.model_validate(
        {
            "id": "decision",
            "outcome": PolicyOutcome.ALLOW,
            "binding": binding(),
            "rule_refs": ("rule",),
            "reason_codes": ("approved",),
            "explanation": "Approved source and destination",
            "policy_version": DIGEST,
            "evaluated_at": 1,
            "revalidate_at": 20,
            "audit_ref": "audit",
            **changes,
        }
    )


def metadata(**changes: object) -> ProtectedContentMetadata:
    return ProtectedContentMetadata.model_validate(
        {
            "reference": ProtectedContentRef(
                reference=SourceContentRef(resource=resource()),
                protection_context_ref="context",
                content_digest=binding().content_digest,
                sources=(source(),),
            ),
            "protection_context": context(),
            "provenance": Lineage(edges=(LineageEdge(source=source(), kind=DependencyKind.DATA),)),
            "authorization_snapshot_refs": ("snapshot",),
            "created_at": 1,
            "lifecycle_state": "active",
            **changes,
        }
    )


def proof(**changes: object) -> ObligationProof:
    return ObligationProof.model_validate(
        {
            "obligation_id": "audit-obligation",
            "kind": ObligationKind.AUDIT,
            "target_ref": "audit",
            "evidence_ref": "receipt",
            "verifier_ref": "runtime",
            "binding_digest": binding().digest,
            "policy_version": DIGEST,
            "fulfilled_at": 2,
            **changes,
        }
    )


def test_default_unknown_protection_cannot_release_even_with_allow() -> None:
    unknown = ProtectionContext(id="context", sources=(source(),))
    content = ProtectedContent({"body": "CONTENT-SENTINEL"}, metadata(protection_context=unknown))
    with pytest.raises(ProtectionDeniedError):
        content.release(PolicyPermit(decision(), authority=TestAuthority(), now=10), binding(), now=10)


@pytest.mark.parametrize("outcome", list(PolicyOutcome)[1:])
def test_only_allow_can_become_a_permit(outcome: PolicyOutcome) -> None:
    with pytest.raises(ProtectionDeniedError):
        PolicyPermit(decision(outcome=outcome), authority=TestAuthority(), now=10)


@pytest.mark.parametrize("now", [0, 20, -1, float("nan"), float("inf")])
def test_permit_rejects_expired_future_and_nonfinite_time(now: float) -> None:
    with pytest.raises(ProtectionDeniedError):
        PolicyPermit(decision(), authority=TestAuthority(), now=now)


@pytest.mark.parametrize(
    "changes",
    [
        {"step_id": "other"},
        {"actor_id": "other"},
        {"content_digest": "b" * 64},
        {"action": DataAction.EXPORT},
        {"destination": DataDestination.NETWORK_ENDPOINT},
        {"sources": (source(source_generation=1),)},
        {"policy_version": "b" * 64},
        {"identities": (identity_binding().model_copy(update={"identity_revision": 2}),)},
        {"connections": (connection_binding().model_copy(update={"connection_revision": 2}),)},
    ],
)
def test_permit_rejects_rebound_content_actor_generation_and_destinations(changes) -> None:
    permit = PolicyPermit(decision(), authority=TestAuthority(), now=10)
    with pytest.raises(ProtectionDeniedError):
        permit.consume(binding(**changes), now=10)


def test_live_authority_revocation_and_one_use_are_enforced_at_release() -> None:
    authority = TestAuthority()
    permit = PolicyPermit(decision(), authority=authority, now=10)
    authority.revoked = True
    with pytest.raises(ProtectionDeniedError, match="revoked"):
        permit.consume(binding(), now=10)
    authority.revoked = False
    with pytest.raises(ProtectionDeniedError):
        permit.consume(binding(), now=10)
    permit = PolicyPermit(decision(), authority=authority, now=10)
    content = ProtectedContent({"body": "CONTENT-SENTINEL"}, metadata())
    assert content.release(permit, binding(), now=10) == {"body": "CONTENT-SENTINEL"}
    assert authority.calls == 4
    with pytest.raises(ProtectionDeniedError):
        permit.consume(binding(), now=10)
    with pytest.raises(ProtectionDeniedError):
        content.release(PolicyPermit(decision(), authority=authority, now=10), binding(), now=10)


@pytest.mark.parametrize(
    "proofs",
    [
        (),
        (proof(), proof()),
        (proof(obligation_id="other"),),
        (proof(target_ref="other"),),
        (proof(kind=ObligationKind.ENCRYPT_AT_REST),),
        (proof(binding_digest="b" * 64),),
        (proof(policy_version="b" * 64),),
        (proof(fulfilled_at=11),),
    ],
)
def test_missing_duplicate_mismatched_or_future_obligations_are_rejected(proofs) -> None:
    required = decision(
        obligations=(
            PolicyObligation(
                id="audit-obligation",
                kind=ObligationKind.AUDIT,
                target_ref="audit",
            ),
        )
    )
    with pytest.raises(ProtectionDeniedError):
        PolicyPermit(required, proofs=proofs, authority=TestAuthority(), now=10)


def test_obligation_requires_runtime_authenticity_not_just_well_formed_evidence() -> None:
    required = decision(
        obligations=(
            PolicyObligation(
                id="audit-obligation",
                kind=ObligationKind.AUDIT,
                target_ref="audit",
            ),
        )
    )
    authority = TestAuthority()
    permit = PolicyPermit(required, proofs=(proof(),), authority=authority, now=10)
    authority.proof_rejected = True
    with pytest.raises(ProtectionDeniedError, match="authentic"):
        permit.consume(binding(), now=10)


@pytest.mark.parametrize("serialize", [json.dumps, pickle.dumps, copy.copy, copy.deepcopy, vars])
def test_protected_body_has_no_default_serialization(serialize) -> None:
    content = ProtectedContent({"body": "CONTENT-SENTINEL"}, metadata())
    assert "CONTENT-SENTINEL" not in repr(content)
    assert "CONTENT-SENTINEL" not in content.metadata.model_dump_json()
    with pytest.raises(TypeError):
        serialize(content)


def test_borrowed_mutable_body_is_rechecked_before_release() -> None:
    body = {"body": "CONTENT-SENTINEL"}
    content = ProtectedContent(body, metadata())
    body["body"] = "changed"
    with pytest.raises(ProtectionDeniedError):
        content.release(PolicyPermit(decision(), authority=TestAuthority(), now=10), binding(), now=10)
    with pytest.raises(ValueError):
        ProtectedContent(body, metadata())


def test_context_cannot_be_swapped_under_the_same_reference_and_policy_version() -> None:
    content = ProtectedContent(
        {"body": "CONTENT-SENTINEL"}, metadata(protection_context=context(classification_id="public"))
    )
    with pytest.raises(ProtectionDeniedError):
        content.release(PolicyPermit(decision(), authority=TestAuthority(), now=10), binding(), now=10)


def test_permit_is_claimed_before_reentrant_authority_validation() -> None:
    class ReenteringAuthority(TestAuthority):
        permit = None

        def validate_decision(self, decision):
            if self.permit is not None:
                with pytest.raises(ProtectionDeniedError):
                    self.permit.consume(binding(), now=10)

    authority = ReenteringAuthority()
    permit = PolicyPermit(decision(), authority=authority, now=10)
    authority.permit = permit
    permit.consume(binding(), now=10)


def test_content_with_expired_transient_handle_cannot_release() -> None:
    reference = ProtectedContentRef(
        reference=TransientContentHandle(handle_id="handle", process_instance_id="process", expires_at=5),
        protection_context_ref="context",
        content_digest=binding().content_digest,
        sources=(source(),),
    )
    content = ProtectedContent({"body": "CONTENT-SENTINEL"}, metadata(reference=reference))
    with pytest.raises(ProtectionDeniedError):
        content.release(PolicyPermit(decision(), authority=TestAuthority(), now=10), binding(), now=10)


def test_durable_refs_never_persist_transient_outputs_or_replay_unavailable_sources() -> None:
    transient = ProtectedContentRef(
        reference=TransientContentHandle(handle_id="handle", process_instance_id="process", expires_at=20),
        protection_context_ref="context",
        content_digest=binding().content_digest,
        sources=(source(),),
    )
    with pytest.raises(ReferenceUnavailableError):
        transient.require_resumable()
    transient.require_available(available=True, now=10, process_instance_id="process")
    with pytest.raises(ReferenceUnavailableError):
        transient.require_available(available=True, now=10, process_instance_id="other")
    for reference in (
        metadata().reference,
        ProtectedContentRef(
            reference=ArtifactRef(
                artifact_id="artifact",
                protection_context_ref="context",
                content_digest=binding().content_digest,
            ),
            protection_context_ref="context",
            content_digest=binding().content_digest,
            sources=(source(),),
        ),
    ):
        reference.require_resumable()
        with pytest.raises(ReferenceUnavailableError):
            reference.require_available(available=False, now=10)
        assert ProtectedContentRef.model_validate_json(reference.model_dump_json()) == reference


def test_merge_retains_control_and_data_lineage_and_rejects_dropped_sources() -> None:
    control = source(resource=resource(resource_id="control"))
    lineage = Lineage(
        edges=(
            LineageEdge(source=source(), kind=DependencyKind.DATA),
            LineageEdge(source=control, kind=DependencyKind.CONTROL),
            LineageEdge(source=control, kind=DependencyKind.DATA),
        )
    )

    class MergePolicy:
        def merge(self, contexts, lineage):
            return context(sources=lineage.sources, derived=True, derivation_operation="transform")

    merged = context().merge(context(sources=(control,)), lineage=lineage, policy=MergePolicy())
    assert set(merged.sources) == {source(), control}
    assert len(lineage.edges) == 3

    class DroppingPolicy:
        def merge(self, contexts, lineage):
            return context(derived=True, derivation_operation="transform")

    with pytest.raises(ProtectionDeniedError):
        context().merge(context(sources=(control,)), lineage=lineage, policy=DroppingPolicy())


def test_binding_and_metadata_revalidate_copied_nested_instances() -> None:
    forged = source().model_copy(update={"resource": resource(tenant_id=OTHER_TENANT)})
    with pytest.raises(ValidationError):
        binding(sources=(forged,))
    with pytest.raises(ValidationError):
        metadata(protection_context=context(sources=(source(resource=resource(source_version="v2")),)))
    with pytest.raises(ValidationError):
        binding(sources=(source().model_copy(update={"source_generation": -1}),))


def test_authorization_snapshot_is_identity_source_version_and_time_bound() -> None:
    snapshot = AuthorizationSnapshot(
        id="snapshot",
        workspace_id="workspace",
        user_id="owner",
        principal_ref="principal",
        identity_profile_id="work",
        identity_revision=1,
        tenant_id=TENANT,
        resource_scope=(resource(),),
        decision="allow",
        captured_at=1,
        policy_version=DIGEST,
        freshness="fresh",
        revalidate_at=20,
    )
    snapshot.require_fresh(identity_binding(), resource(), DIGEST, 10)
    for identity, source_ref, version, now in (
        (identity_binding(), resource(source_version="v2"), DIGEST, 10),
        (identity_binding().model_copy(update={"principal_ref": "other"}), resource(), DIGEST, 10),
        (identity_binding(), resource(), "b" * 64, 10),
        (identity_binding(), resource(), DIGEST, 20),
        (identity_binding().model_copy(update={"workspace_id": "other"}), resource(), DIGEST, 10),
        (identity_binding().model_copy(update={"identity_revision": 2}), resource(), DIGEST, 10),
    ):
        with pytest.raises(ProtectionDeniedError):
            snapshot.require_fresh(identity, source_ref, version, now)


def test_local_models_are_not_automatically_approved_and_exact_model_is_required() -> None:
    model = ModelBinding(
        model_profile_id="model",
        revision=1,
        provider="ollama",
        model_name="reviewed",
        endpoint="http://127.0.0.1:11434",
        environment="local",
    )
    profile = ModelProfile(
        id="model",
        provider="ollama",
        model_name="reviewed",
        endpoint=model.endpoint,
        environment="local",
        retention_characteristics_ref="retention",
        data_boundary_ref="boundary",
    )
    with pytest.raises(ProtectionDeniedError):
        profile.require_allowed(model, context(), DataAction.SEND_TO_MODEL, now=10)
    approved = profile.model_copy(
        update={
            "enterprise_approved": True,
            "enabled": True,
            "approval_policy_ref": "model-policy",
            "permitted_classifications": ("internal",),
            "permitted_actions": (DataAction.SEND_TO_MODEL,),
            "permitted_tenants": (TENANT,),
            "generation_allowed": True,
        }
    )
    approved.require_allowed(model, context(), DataAction.SEND_TO_MODEL, now=10)
    with pytest.raises(ProtectionDeniedError):
        approved.require_allowed(
            model.model_copy(update={"model_name": "unreviewed"}), context(), DataAction.SEND_TO_MODEL, now=10
        )
    with pytest.raises(ValidationError):
        binding(action=DataAction.EMBED)
    with pytest.raises(ValidationError):
        binding(destination=DataDestination.EXTERNAL_MODEL, model=model)
