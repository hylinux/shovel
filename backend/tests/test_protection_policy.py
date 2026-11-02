import json
import traceback

import pytest

from shovel.protection.domain import (
    AuthorizationSnapshot,
    ContentRight,
    DataAction,
    DataDestination,
    DependencyKind,
    Lineage,
    LineageEdge,
    ModelBinding,
    ModelProfile,
    ObligationKind,
    ObligationProof,
    PolicyObligation,
    PolicyOutcome,
    ProtectedContent,
    ProtectedContentRef,
    ProtectionAdapterError,
    ProtectionDeniedError,
    SourceContentRef,
    TransientContentHandle,
)
from shovel.protection.policy import DataRule, PolicyState, ProtectionPolicy, SourceAuthorization
from shovel.protection.service import ConservativeProtectionMerge, ProtectionService
from test_data_protection_contracts import binding, context, decision, metadata, source
from test_enterprise_connection_contracts import resource
from test_enterprise_identity_contracts import DIGEST, OTHER_TENANT, TENANT

BODY = {"body": "CONTENT-SENTINEL"}


def reviewed_context(**changes):
    return context(
        rights=tuple(ContentRight),
        retention_policy_ref="retention",
        expires_at=50,
        revalidate_at=40,
        **changes,
    )


def use_binding(ctx, **changes):
    return binding(
        protection_context_ref=ctx.id,
        protection_context_digest=ctx.content_hash,
        sources=ctx.sources,
        action=changes.pop("action", DataAction.DISPLAY),
        **changes,
    )


def state_for(ctx, use, **changes):
    rules = (
        DataRule(
            id="organization-rule",
            classifications=("internal",),
            action=use.action,
            destination=use.destination,
            outcome=PolicyOutcome.ALLOW,
            retention_policy_refs=("retention",),
        ),
    )
    evidence = tuple(
        SourceAuthorization(
            source=item,
            snapshot=AuthorizationSnapshot(
                id=f"snapshot-{index}",
                workspace_id=use.identities[0].workspace_id,
                user_id=use.identities[0].user_id,
                principal_ref=item.principal_ref,
                identity_profile_id=item.identity_profile_id,
                identity_revision=use.identities[0].identity_revision,
                tenant_id=item.resource.tenant_id,
                resource_scope=(item.resource,),
                decision="allow",
                captured_at=1,
                policy_version=DIGEST,
                freshness="fresh",
                revalidate_at=30,
            ),
            active=True,
            authentication_valid=True,
        )
        for index, item in enumerate(ctx.sources)
    )
    return PolicyState(
        context=ctx,
        binding=use,
        classifications=("internal",),
        sources=evidence,
        rules=changes.pop("rules", rules),
        captured_at=1,
        revalidate_at=25,
        execution_deadline=24,
        **changes,
    )


class Provider:
    def __init__(self, *states):
        self.states = {state.binding.digest: state for state in states}

    def resolve(self, context, binding):
        return self.states[binding.digest]


class Audit:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def kernel(state, *, audit=None, verifier=None):
    provider = Provider(state)
    audit = audit or Audit()
    policy = ProtectionPolicy(audit, provider=provider, verifier=verifier, clock=lambda: 10)
    return policy, provider, audit


def wrapped(ctx):
    return ProtectedContent(
        BODY.copy(),
        metadata(
            reference=ProtectedContentRef(
                reference=SourceContentRef(resource=ctx.sources[0].resource),
                protection_context_ref=ctx.id,
                content_digest=binding().content_digest,
                sources=ctx.sources,
            ),
            protection_context=ctx,
            provenance=Lineage(
                edges=tuple(LineageEdge(source=item, kind=DependencyKind.DATA) for item in ctx.sources)
            ),
        ),
    )


def test_default_policy_has_no_grant_and_cannot_invoke_consumer():
    ctx = reviewed_context()
    use = use_binding(ctx)
    audit = Audit()
    policy = ProtectionPolicy(audit, clock=lambda: 10)
    assert policy.evaluate(ctx, use).reason_codes == ("policy_unavailable",)
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, lambda _: pytest.fail("Released"))
    assert all(event.outcome == PolicyOutcome.DENY for event in audit.events)


def test_issued_permit_rechecks_live_authority_and_cannot_be_forged_or_reissued():
    ctx = reviewed_context()
    use = use_binding(ctx)
    state = state_for(ctx, use)
    policy, provider, audit = kernel(state)
    issued = policy.evaluate(ctx, use)
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(issued.model_copy(update={"audit_ref": "forged"}))
    permit = policy.authorize(issued)
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(issued)
    provider.states[use.digest] = state.model_copy(update={"rules": ()})
    with pytest.raises(ProtectionDeniedError):
        wrapped(ctx).release(permit, use, now=10)
    provider.states[use.digest] = state
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(issued)
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(decision(binding=use))
    assert "CONTENT-SENTINEL" not in json.dumps([event.model_dump(mode="json") for event in audit.events])
    assert ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, lambda value: value) == BODY


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"classification_id": None}, "protection_unknown_or_stale"),
        ({"classification_id": "unrecognized"}, "classification_unknown"),
        ({"rights": (ContentRight.READ,)}, "content_right_missing"),
        ({"restrictions": ("unhandled",)}, "restriction_unsupported"),
        ({"dlp_applicable": True, "dlp_decision_ref": "dlp"}, "dlp_not_allowed"),
        ({"protection_status": "unknown"}, "protection_unknown_or_stale"),
    ],
)
def test_unknown_protection_rights_and_dlp_never_release(changes, reason):
    ctx = reviewed_context().model_copy(update=changes)
    use = use_binding(ctx)
    policy, _, _ = kernel(state_for(ctx, use))
    assert policy.evaluate(ctx, use).reason_codes == (reason,)
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, lambda _: pytest.fail("Released"))


@pytest.mark.parametrize(
    "alter",
    [
        lambda state: state.model_copy(update={"sources": ()}),
        lambda state: state.model_copy(
            update={"binding": state.binding.model_copy(update={"actor_id": "other"})}
        ),
        lambda state: state.model_copy(
            update={"sources": (state.sources[0].model_copy(update={"active": False}),)}
        ),
    ],
)
def test_incomplete_or_unreviewed_source_actor_binding_denies(alter):
    ctx = reviewed_context()
    use = use_binding(ctx)
    policy, _, _ = kernel(alter(state_for(ctx, use)))
    assert policy.evaluate(ctx, use).outcome == PolicyOutcome.DENY


@pytest.mark.parametrize(
    "field,value,outcome",
    [
        ("authentication_valid", False, PolicyOutcome.REQUIRE_REAUTHENTICATION),
        ("snapshot", "stale", PolicyOutcome.REQUIRE_SOURCE_REVALIDATION),
        ("snapshot", "denied", PolicyOutcome.DENY),
    ],
)
def test_authentication_and_source_revalidation_are_not_allow(field, value, outcome):
    ctx = reviewed_context()
    use = use_binding(ctx)
    state = state_for(ctx, use)
    if field == "snapshot":
        value = state.sources[0].snapshot.model_copy(
            update={"freshness": "stale"} if value == "stale" else {"decision": "deny"}
        )
    state = state.model_copy(update={"sources": (state.sources[0].model_copy(update={field: value}),)})
    policy, _, _ = kernel(state)
    assert policy.evaluate(ctx, use).outcome == outcome
    with pytest.raises(ProtectionDeniedError):
        policy.permit(ctx, use)


@pytest.mark.parametrize("outcome", list(PolicyOutcome)[1:])
def test_explicit_deny_and_requirements_win_over_allow(outcome):
    ctx = reviewed_context()
    use = use_binding(ctx)
    state = state_for(ctx, use)
    state = state.model_copy(
        update={
            "rules": (
                *state.rules,
                state.rules[0].model_copy(update={"id": "restrictive-rule", "outcome": outcome}),
            )
        }
    )
    policy, _, _ = kernel(state)
    assert policy.evaluate(ctx, use).outcome == outcome
    with pytest.raises(ProtectionDeniedError):
        policy.permit(ctx, use)


@pytest.mark.parametrize(
    "action,destination",
    [
        (DataAction.PERSIST, DataDestination.LOCAL_SQLITE),
        (DataAction.INDEX, DataDestination.LOCAL_ZVEC),
        (DataAction.WRITE_TO_MEMORY, DataDestination.MEMORY_STORE),
        (DataAction.WRITE_TO_KNOWLEDGE, DataDestination.KNOWLEDGE_STORE),
        (DataAction.EXPORT, DataDestination.NETWORK_ENDPOINT),
        (DataAction.COPY_TO_CLIPBOARD, DataDestination.WINDOWS_CLIPBOARD),
    ],
)
def test_display_grant_never_authorizes_a_different_sink(action, destination):
    ctx = reviewed_context()
    use = use_binding(ctx, action=action, destination=destination)
    state = state_for(ctx, use, rules=state_for(ctx, use_binding(ctx)).rules)
    policy, _, _ = kernel(state)
    sent = []
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, sent.append)
    assert not sent


def test_read_cannot_masquerade_as_display_and_local_storage_requires_retention():
    ctx = reviewed_context()
    read = use_binding(ctx, action=DataAction.READ)
    policy, _, _ = kernel(state_for(ctx, read))
    assert policy.evaluate(ctx, read).reason_codes == ("action_destination_mismatch",)
    ctx = ctx.model_copy(update={"retention_policy_ref": None})
    persist = use_binding(ctx, action=DataAction.PERSIST, destination=DataDestination.LOCAL_SQLITE)
    policy, _, _ = kernel(state_for(ctx, persist))
    assert policy.evaluate(ctx, persist).reason_codes == ("retention_not_approved",)


@pytest.mark.parametrize("action", [DataAction.EMBED, DataAction.SEND_TO_MODEL, DataAction.PERSIST])
def test_read_grant_cannot_be_reused_for_an_approved_model_or_sqlite_sink(action):
    ctx = reviewed_context()
    model = ModelBinding(
        model_profile_id="approved-model",
        revision=1,
        provider="synthetic",
        model_name="reviewed",
        endpoint="https://model.example",
        environment="test",
    )
    profile = ModelProfile(
        id=model.model_profile_id,
        provider=model.provider,
        model_name=model.model_name,
        endpoint=model.endpoint,
        environment=model.environment,
        enterprise_approved=True,
        approval_policy_ref="organization-model-policy",
        permitted_classifications=("internal",),
        permitted_actions=(DataAction.EMBED, DataAction.SEND_TO_MODEL),
        permitted_tenants=(TENANT,),
        retention_characteristics_ref="retention",
        data_boundary_ref="boundary",
        embedding_allowed=True,
        generation_allowed=True,
        enabled=True,
    )
    read = use_binding(ctx, action=DataAction.READ, destination=DataDestination.WORKFLOW_STATE)
    read_state = state_for(ctx, read)
    target = use_binding(
        ctx,
        action=action,
        destination=DataDestination.LOCAL_SQLITE
        if action == DataAction.PERSIST
        else DataDestination.ENTERPRISE_MODEL,
        model=None if action == DataAction.PERSIST else model,
    )
    provider = Provider(read_state, state_for(ctx, target, rules=read_state.rules, models=(profile,)))
    audit = Audit()
    policy = ProtectionPolicy(audit, provider=provider, clock=lambda: 10)
    service = ProtectionService(policy, clock=lambda: 10)
    assert service.use(wrapped(ctx), read, lambda body: body) == BODY
    released = []
    with pytest.raises(ProtectionDeniedError):
        service.use(wrapped(ctx), target, released.append)
    assert not released
    assert audit.events[-1].reason_codes == ("no_unambiguous_rule",)


@pytest.mark.parametrize("action", [DataAction.SEND_TO_MODEL, DataAction.EMBED])
def test_model_and_embedding_sinks_require_exact_reviewed_model(action):
    ctx = reviewed_context()
    model = ModelBinding(
        model_profile_id="model",
        revision=1,
        provider="ollama",
        model_name="reviewed",
        endpoint="http://127.0.0.1:11434",
        environment="local",
    )
    use = use_binding(ctx, action=action, destination=DataDestination.ENTERPRISE_MODEL, model=model)
    state = state_for(ctx, use)
    policy, provider, _ = kernel(state)
    called = []
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, called.append)
    assert called == []
    profile = ModelProfile(
        id=model.model_profile_id,
        provider=model.provider,
        model_name=model.model_name,
        endpoint=model.endpoint,
        environment=model.environment,
        enterprise_approved=True,
        approval_policy_ref="organization-model-policy",
        permitted_classifications=("internal",),
        permitted_actions=(action,),
        permitted_tenants=(TENANT,),
        retention_characteristics_ref="retention",
        data_boundary_ref="boundary",
        embedding_allowed=True,
        generation_allowed=True,
        enabled=True,
    )
    provider.states[use.digest] = state.model_copy(update={"models": (profile,)})
    ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, called.append)
    assert called == [BODY]


def test_audit_and_policy_provider_failures_are_safe_and_no_sink_runs():
    ctx = reviewed_context()
    use = use_binding(ctx)

    class FailedAudit:
        def record(self, event):
            raise RuntimeError("CONTENT-SENTINEL")

    policy, _, _ = kernel(state_for(ctx, use), audit=FailedAudit())
    with pytest.raises(ProtectionDeniedError) as caught:
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, lambda _: pytest.fail("Released"))
    assert "CONTENT-SENTINEL" not in "".join(traceback.format_exception(caught.value))

    class FailedProvider:
        def resolve(self, context, binding):
            raise RuntimeError("CONTENT-SENTINEL")

    policy = ProtectionPolicy(Audit(), provider=FailedProvider(), clock=lambda: 10)
    assert policy.evaluate(ctx, use).reason_codes == ("policy_unavailable",)


def test_obligations_require_trusted_verification_at_issuance_and_release():
    ctx = reviewed_context()
    use = use_binding(ctx)
    obligation = PolicyObligation(id="audit-required", kind=ObligationKind.AUDIT, target_ref="audit-target")
    state = state_for(ctx, use)
    state = state.model_copy(
        update={"rules": (state.rules[0].model_copy(update={"obligations": (obligation,)}),)}
    )

    class Verifier:
        valid = True

        def validate(self, proof, decision):
            if not self.valid:
                raise RuntimeError("CONTENT-SENTINEL")

    verifier = Verifier()
    policy, _, _ = kernel(state, verifier=verifier)
    issued = policy.evaluate(ctx, use)
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(issued)
    issued = policy.evaluate(ctx, use)
    proof = ObligationProof(
        obligation_id=obligation.id,
        kind=obligation.kind,
        target_ref=obligation.target_ref,
        evidence_ref="trusted-receipt",
        verifier_ref="trusted-verifier",
        binding_digest=use.digest,
        policy_version=DIGEST,
        fulfilled_at=10,
    )
    permit = policy.authorize(issued, proofs=(proof,))
    verifier.valid = False
    with pytest.raises(ProtectionDeniedError):
        wrapped(ctx).release(permit, use, now=10)


def test_expired_decision_and_changed_source_generation_reject_before_sink():
    ctx = reviewed_context()
    use = use_binding(ctx)
    policy, provider, _ = kernel(state_for(ctx, use))
    issued = policy.evaluate(ctx, use)
    policy.clock = lambda: 25
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(issued)
    policy.clock = lambda: 10
    permit = policy.permit(ctx, use)
    provider.states[use.digest] = provider.states[use.digest].model_copy(
        update={
            "sources": (
                provider.states[use.digest]
                .sources[0]
                .model_copy(update={"source": ctx.sources[0].model_copy(update={"source_generation": 1})}),
            )
        }
    )
    with pytest.raises(ProtectionDeniedError):
        wrapped(ctx).release(permit, use, now=10)


def test_derivation_preserves_control_and_data_lineage_and_requires_new_egress_decision():
    first = reviewed_context()
    second = first.model_copy(
        update={
            "id": "control-context",
            "sources": (source(resource=resource(resource_id="control")),),
            "rights": (ContentRight.READ, ContentRight.TRANSFORM),
            "restrictions": ("no-share",),
            "expires_at": 20,
        }
    )
    uses = tuple(
        use_binding(ctx, action=DataAction.TRANSFORM, destination=DataDestination.WORKFLOW_STATE)
        for ctx in (first, second)
    )
    states = [state_for(ctx, use) for ctx, use in zip((first, second), uses, strict=True)]
    states[1] = states[1].model_copy(
        update={"rules": (states[1].rules[0].model_copy(update={"permitted_restrictions": ("no-share",)}),)}
    )
    policy = ProtectionPolicy(Audit(), provider=Provider(*states), clock=lambda: 10)
    service = ProtectionService(policy, clock=lambda: 10)
    output = service.derive(
        ((wrapped(first), uses[0], DependencyKind.DATA), (wrapped(second), uses[1], DependencyKind.CONTROL)),
        "transform",
        lambda values: {"derived": len(values)},
    )
    merged = output.metadata.protection_context
    assert merged.derived and merged.derivation_operation == "transform"
    assert set(merged.sources) == set(first.sources + second.sources)
    assert set(merged.rights) == {ContentRight.READ, ContentRight.TRANSFORM}
    assert merged.restrictions == ("no-share",)
    assert merged.expires_at == 20
    assert (
        LineageEdge(source=second.sources[0], kind=DependencyKind.CONTROL) in output.metadata.provenance.edges
    )
    assert LineageEdge(source=second.sources[0], kind=DependencyKind.DATA) in output.metadata.provenance.edges
    assert "CONTENT-SENTINEL" not in output.metadata.model_dump_json()
    with pytest.raises(TypeError):
        json.dumps(output)
    use = use_binding(merged, content_digest=output.metadata.reference.content_digest)
    with pytest.raises(ProtectionDeniedError):
        service.use(output, use, lambda _: pytest.fail("Unreviewed derived content escaped."))


@pytest.mark.parametrize(
    "changes",
    [
        {"classification_id": "other"},
        {"policy_version": "b" * 64},
        {"sources": (source(resource=resource(tenant_id=OTHER_TENANT)),)},
        {"protection_status": "unknown"},
    ],
)
def test_conservative_merge_refuses_guessed_policy_or_cross_tenant_order(changes):
    first = reviewed_context()
    second = first.model_copy(update={"id": "other", **changes})
    lineage = Lineage(
        edges=tuple(
            LineageEdge(source=item, kind=DependencyKind.DATA)
            for item in dict.fromkeys(first.sources + second.sources)
        )
    )
    with pytest.raises(ProtectionDeniedError):
        first.merge(
            second, lineage=lineage, policy=ConservativeProtectionMerge("transform", clock=lambda: 10)
        )


def test_denied_derive_never_invokes_transform_and_encrypted_content_is_not_treated_as_plaintext():
    ctx = reviewed_context()
    use = use_binding(ctx, action=DataAction.TRANSFORM, destination=DataDestination.WORKFLOW_STATE)
    policy, _, _ = kernel(state_for(ctx, use, rules=()))
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).derive(
            ((wrapped(ctx), use, DependencyKind.DATA),), "transform", lambda _: pytest.fail("Transform ran.")
        )
    ctx = ctx.model_copy(update={"encrypted": True, "protection_status": "protected"})
    with pytest.raises(ProtectionDeniedError):
        ProtectionService(policy, clock=lambda: 10).derive(
            ((wrapped(ctx), use, DependencyKind.DATA),),
            "transform",
            lambda _: pytest.fail("No protected handler."),
        )


def test_protected_display_needs_an_implemented_and_verified_obligation():
    ctx = reviewed_context().model_copy(update={"encrypted": True, "protection_status": "protected"})
    use = use_binding(ctx)
    state = state_for(ctx, use)
    policy, _, _ = kernel(state)
    assert policy.evaluate(ctx, use).outcome == PolicyOutcome.REQUIRE_PROTECTION
    obligation = PolicyObligation(
        id="protected-view", kind=ObligationKind.PROTECTED_DISPLAY, target_ref="trusted-viewer"
    )
    state = state.model_copy(
        update={"rules": (state.rules[0].model_copy(update={"obligations": (obligation,)}),)}
    )
    policy, _, _ = kernel(state)
    proof = ObligationProof(
        obligation_id=obligation.id,
        kind=obligation.kind,
        target_ref=obligation.target_ref,
        evidence_ref="view-receipt",
        verifier_ref="unknown-viewer",
        binding_digest=use.digest,
        policy_version=DIGEST,
        fulfilled_at=10,
    )
    with pytest.raises(ProtectionDeniedError):
        policy.authorize(policy.evaluate(ctx, use), proofs=(proof,))


def test_adapter_errors_are_sanitized_without_retrying_an_uncertain_effect():
    ctx = reviewed_context()
    use = use_binding(ctx)
    policy, _, _ = kernel(state_for(ctx, use))
    calls = []

    def failed_consumer(value):
        calls.append(value)
        raise RuntimeError("CONTENT-SENTINEL")

    with pytest.raises(ProtectionAdapterError) as caught:
        ProtectionService(policy, clock=lambda: 10).use(wrapped(ctx), use, failed_consumer)
    assert calls == [BODY]
    assert "CONTENT-SENTINEL" not in "".join(traceback.format_exception(caught.value))


def test_derived_output_cannot_outlive_a_source():
    ctx = reviewed_context()
    use = use_binding(ctx, action=DataAction.SUMMARIZE, destination=DataDestination.WORKFLOW_STATE)
    policy, _, _ = kernel(state_for(ctx, use))
    now = [10]
    service = ProtectionService(policy, clock=lambda: now[0])

    def transform(values):
        assert values == (BODY,)
        now[0] = 50
        return "complete-buffer"

    with pytest.raises(ProtectionDeniedError):
        service.derive(((wrapped(ctx), use, DependencyKind.DATA),), "summarize", transform)


def test_derived_result_remains_transient_and_source_bound_after_json_metadata_roundtrip():
    ctx = reviewed_context()
    use = use_binding(ctx, action=DataAction.TRANSFORM, destination=DataDestination.WORKFLOW_STATE)
    policy, _, _ = kernel(state_for(ctx, use))
    result = ProtectionService(policy, clock=lambda: 10).derive(
        ((wrapped(ctx), use, DependencyKind.DATA),),
        "transform",
        lambda values: {"result": True},
    )
    copied = type(result.metadata).model_validate_json(result.metadata.model_dump_json())
    assert copied == result.metadata
    with pytest.raises(ValueError):
        copied.reference.require_resumable()
    assert copied.protection_context.delete_with_source


def test_source_revalidation_crossing_the_deadline_cannot_release_content():
    ctx = reviewed_context()
    use = use_binding(ctx)
    state = state_for(ctx, use)
    now = [10]

    class SlowProvider(Provider):
        slow = False

        def resolve(self, context, binding):
            if self.slow:
                now[0] = 25
            return super().resolve(context, binding)

    provider = SlowProvider(state)
    policy = ProtectionPolicy(Audit(), provider=provider, clock=lambda: now[0])
    permit = policy.permit(ctx, use)
    provider.slow = True
    with pytest.raises(ProtectionDeniedError):
        wrapped(ctx).release(permit, use, now=10)


def test_slow_obligation_verification_cannot_extend_execution_budget():
    ctx = reviewed_context()
    use = use_binding(ctx)
    state = state_for(ctx, use)
    obligation = PolicyObligation(id="audit-required", kind=ObligationKind.AUDIT, target_ref="audit-target")
    state = state.model_copy(
        update={"rules": (state.rules[0].model_copy(update={"obligations": (obligation,)}),)}
    )
    now = [10]

    class SlowVerifier:
        slow = False

        def validate(self, proof, decision):
            if self.slow:
                now[0] = 24

    verifier = SlowVerifier()
    policy = ProtectionPolicy(Audit(), provider=Provider(state), verifier=verifier, clock=lambda: now[0])
    issued = policy.evaluate(ctx, use)
    proof = ObligationProof(
        obligation_id=obligation.id,
        kind=obligation.kind,
        target_ref=obligation.target_ref,
        evidence_ref="receipt",
        verifier_ref="trusted-verifier",
        binding_digest=use.digest,
        policy_version=DIGEST,
        fulfilled_at=10,
    )
    permit = policy.authorize(issued, proofs=(proof,))
    verifier.slow = True
    with pytest.raises(ProtectionDeniedError):
        wrapped(ctx).release(permit, use, now=10)


def test_new_service_instance_cannot_recover_a_transient_derived_buffer():
    ctx = reviewed_context()
    use = use_binding(ctx, action=DataAction.TRANSFORM, destination=DataDestination.WORKFLOW_STATE)
    policy, _, _ = kernel(state_for(ctx, use))
    result = ProtectionService(policy, clock=lambda: 10).derive(
        ((wrapped(ctx), use, DependencyKind.DATA),),
        "transform",
        lambda _: "derived",
    )
    output_use = use_binding(
        result.metadata.protection_context,
        content_digest=result.metadata.reference.content_digest,
    )
    with pytest.raises(ValueError):
        ProtectionService(policy, clock=lambda: 10).use(
            result, output_use, lambda _: pytest.fail("Recovered.")
        )


def test_body_mutated_during_live_policy_recheck_cannot_be_released():
    ctx = reviewed_context()
    use = use_binding(ctx)
    body = BODY.copy()

    class MutatingProvider(Provider):
        mutate = False

        def resolve(self, context, binding):
            if self.mutate:
                body["body"] = "changed"
            return super().resolve(context, binding)

    provider = MutatingProvider(state_for(ctx, use))
    policy = ProtectionPolicy(Audit(), provider=provider, clock=lambda: 10)
    content = ProtectedContent(body, metadata(protection_context=ctx))
    permit = policy.permit(ctx, use)
    provider.mutate = True
    with pytest.raises(ProtectionDeniedError):
        content.release(permit, use, now=10)


def test_transient_expiring_during_live_policy_recheck_never_reaches_the_sink():
    ctx = reviewed_context()
    use = use_binding(ctx)
    now = [10]

    class SlowProvider(Provider):
        slow = False

        def resolve(self, context, binding):
            if self.slow:
                now[0] = 12
            return super().resolve(context, binding)

    provider = SlowProvider(state_for(ctx, use))

    class DelayedPolicy(ProtectionPolicy):
        def permit(self, context, binding, *, proofs=()):
            permit = super().permit(context, binding, proofs=proofs)
            provider.slow = True
            return permit

    policy = DelayedPolicy(Audit(), provider=provider, clock=lambda: now[0])
    service = ProtectionService(policy, clock=lambda: now[0])
    ref = wrapped(ctx).metadata.reference.model_copy(
        update={
            "reference": TransientContentHandle(
                handle_id="buffer",
                process_instance_id=service.process_instance_id,
                expires_at=11,
            ),
        }
    )
    content = ProtectedContent(BODY.copy(), metadata(protection_context=ctx, reference=ref))
    with pytest.raises(ValueError):
        service.use(content, use, lambda _: pytest.fail("Expired body."))
