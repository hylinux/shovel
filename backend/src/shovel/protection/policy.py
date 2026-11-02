import time
from collections.abc import Callable
from threading import RLock
from typing import Protocol
from uuid import uuid4

import structlog
from pydantic import Field

from shovel.capability.domain import Digest, Identifier
from shovel.identity.domain import UnixSeconds
from shovel.protection.domain import (
    AuthorizationSnapshot,
    ContentRight,
    DataAction,
    DataDestination,
    DataUseBinding,
    ModelProfile,
    ObligationKind,
    ObligationProof,
    PolicyDecision,
    PolicyObligation,
    PolicyOutcome,
    PolicyPermit,
    ProtectionContext,
    ProtectionDeniedError,
    ProtectionValue,
    SourceBinding,
)


class DataRule(ProtectionValue):
    id: Identifier
    classifications: tuple[Identifier, ...] = Field(min_length=1)
    action: DataAction
    destination: DataDestination
    outcome: PolicyOutcome = PolicyOutcome.DENY
    permitted_restrictions: tuple[Identifier, ...] = ()
    retention_policy_refs: tuple[Identifier, ...] = ()
    obligations: tuple[PolicyObligation, ...] = ()


class SourceAuthorization(ProtectionValue):
    source: SourceBinding
    snapshot: AuthorizationSnapshot
    active: bool = Field(default=False, strict=True)
    authentication_valid: bool = Field(default=False, strict=True)


class PolicyState(ProtectionValue):
    """Trusted adapter's exact reviewed inputs, never a Provider/Agent supplied policy."""

    context: ProtectionContext
    binding: DataUseBinding
    classifications: tuple[Identifier, ...] = ()
    sources: tuple[SourceAuthorization, ...] = ()
    rules: tuple[DataRule, ...] = ()
    models: tuple[ModelProfile, ...] = ()
    dlp_allowed: bool = Field(default=False, strict=True)
    captured_at: UnixSeconds
    revalidate_at: UnixSeconds
    execution_deadline: UnixSeconds


class PolicyStateProvider(Protocol):
    def resolve(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyState: ...


class PolicyAudit(ProtectionValue):
    decision_id: Identifier
    outcome: PolicyOutcome
    action: DataAction
    destination: DataDestination
    rule_refs: tuple[Identifier, ...]
    reason_codes: tuple[Identifier, ...]
    policy_version: Digest
    evaluated_at: UnixSeconds
    audit_ref: Identifier


class PolicyAuditSink(Protocol):
    """Raise on unavailable/failed audit; receipt persistence is adapter-owned."""

    def record(self, event: PolicyAudit) -> None: ...


class ObligationVerifier(Protocol):
    def validate(self, proof: ObligationProof, decision: PolicyDecision) -> None: ...


_RIGHTS = {
    DataAction.READ: ContentRight.READ,
    DataAction.DISPLAY: ContentRight.DISPLAY,
    DataAction.PROCESS: ContentRight.TRANSFORM,
    DataAction.SUMMARIZE: ContentRight.SUMMARIZE,
    DataAction.EXTRACT: ContentRight.EXTRACT,
    DataAction.TRANSFORM: ContentRight.TRANSFORM,
    DataAction.SEND_TO_MODEL: ContentRight.SEND_TO_MODEL,
    DataAction.EMBED: ContentRight.EMBED,
    DataAction.CACHE: ContentRight.PERSIST,
    DataAction.INDEX: ContentRight.PERSIST,
    DataAction.PERSIST: ContentRight.PERSIST,
    DataAction.WRITE_TO_MEMORY: ContentRight.PERSIST,
    DataAction.WRITE_TO_KNOWLEDGE: ContentRight.PERSIST,
    DataAction.EXPORT: ContentRight.EXPORT,
    DataAction.SHARE: ContentRight.SHARE,
    DataAction.COPY_TO_CLIPBOARD: ContentRight.COPY,
    DataAction.WRITE_TO_FILE: ContentRight.EXPORT,
    DataAction.SEND_TO_PROVIDER: ContentRight.SHARE,
}
_STORES = {
    DataDestination.LOCAL_SQLITE,
    DataDestination.LOCAL_ZVEC,
    DataDestination.LOCAL_ARTIFACT_STORE,
    DataDestination.LOCAL_FILE_SYSTEM,
    DataDestination.MEMORY_STORE,
    DataDestination.KNOWLEDGE_STORE,
    DataDestination.WORKFLOW_STATE,
    DataDestination.APPLICATION_LOG,
    DataDestination.AUDIT_STORE,
}
_DESTINATIONS = {
    DataAction.READ: {DataDestination.WORKFLOW_STATE},
    DataAction.PROCESS: {DataDestination.WORKFLOW_STATE},
    DataAction.TRANSFORM: {DataDestination.WORKFLOW_STATE},
    DataAction.SUMMARIZE: {DataDestination.WORKFLOW_STATE},
    DataAction.EXTRACT: {DataDestination.WORKFLOW_STATE},
    DataAction.DISPLAY: {DataDestination.USER_SCREEN},
    DataAction.COPY_TO_CLIPBOARD: {DataDestination.WINDOWS_CLIPBOARD},
    DataAction.SEND_TO_MODEL: {DataDestination.ENTERPRISE_MODEL, DataDestination.EXTERNAL_MODEL},
    DataAction.EMBED: {DataDestination.ENTERPRISE_MODEL, DataDestination.EXTERNAL_MODEL},
    DataAction.CACHE: _STORES,
    DataAction.PERSIST: _STORES,
    DataAction.INDEX: {DataDestination.LOCAL_ZVEC},
    DataAction.WRITE_TO_MEMORY: {DataDestination.MEMORY_STORE},
    DataAction.WRITE_TO_KNOWLEDGE: {DataDestination.KNOWLEDGE_STORE},
    DataAction.WRITE_TO_FILE: {DataDestination.LOCAL_FILE_SYSTEM},
    DataAction.EXPORT: {
        DataDestination.LOCAL_FILE_SYSTEM,
        DataDestination.LOCAL_ARTIFACT_STORE,
        DataDestination.NETWORK_ENDPOINT,
    },
    DataAction.SHARE: {DataDestination.ENTERPRISE_PROVIDER, DataDestination.NETWORK_ENDPOINT},
    DataAction.SEND_TO_PROVIDER: {DataDestination.ENTERPRISE_PROVIDER},
}


def _failure(where: str, error: Exception) -> None:
    structlog.get_logger("shovel.protection").warning(
        "protection.adapter_failure",
        adapter=where,
        error_type=type(error).__name__,
    )


class ProtectionPolicy:
    """Fail-closed kernel and process-local permit authority; no production policy trust root."""

    def __init__(
        self,
        audit: PolicyAuditSink,
        *,
        provider: PolicyStateProvider | None = None,
        verifier: ObligationVerifier | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.audit, self.provider, self.verifier, self.clock = audit, provider, verifier, clock
        self._issued: dict[str, tuple[PolicyDecision, PolicyState]] = {}
        self._claimed: set[str] = set()
        self._lock = RLock()

    def _resolve(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyState | None:
        if self.provider is None:
            return None
        try:
            return PolicyState.model_validate(self.provider.resolve(context, binding))
        except Exception as error:
            _failure("policy_state", error)
            return None

    def _assess(
        self,
        context: ProtectionContext,
        binding: DataUseBinding,
        state: PolicyState | None,
        now: float,
    ) -> tuple[PolicyOutcome, str, tuple[DataRule, ...]]:
        if state is None:
            return PolicyOutcome.DENY, "policy_unavailable", ()
        if binding.destination not in _DESTINATIONS[binding.action]:
            return PolicyOutcome.DENY, "action_destination_mismatch", ()
        if (
            state.context != context
            or state.binding != binding
            or binding.policy_version != context.policy_version
            or binding.protection_context_ref != context.id
            or binding.protection_context_digest != context.content_hash
            or set(binding.sources) != set(context.sources)
        ):
            return PolicyOutcome.DENY, "untrusted_binding", ()
        if not state.captured_at <= now < state.revalidate_at:
            return PolicyOutcome.REQUIRE_SOURCE_REVALIDATION, "policy_state_stale", ()
        if now >= state.execution_deadline:
            return PolicyOutcome.DENY, "execution_deadline_exceeded", ()
        try:
            context.require_known(now)
        except ProtectionDeniedError:
            return PolicyOutcome.DENY, "protection_unknown_or_stale", ()
        if context.classification_id not in state.classifications:
            return PolicyOutcome.DENY, "classification_unknown", ()
        if len(state.sources) != len(context.sources) or {entry.source for entry in state.sources} != set(
            context.sources
        ):
            return PolicyOutcome.DENY, "source_evidence_missing", ()
        for entry in state.sources:
            if not entry.active:
                return PolicyOutcome.DENY, "source_inactive", ()
            if not entry.authentication_valid:
                return PolicyOutcome.REQUIRE_REAUTHENTICATION, "authentication_required", ()
            identity = next(
                item
                for item in binding.identities
                if item.identity_profile_id == entry.source.identity_profile_id
            )
            try:
                entry.snapshot.require_fresh(identity, entry.source.resource, binding.policy_version, now)
            except ProtectionDeniedError:
                if entry.snapshot.decision == "deny":
                    return PolicyOutcome.DENY, "source_authorization_denied", ()
                return PolicyOutcome.REQUIRE_SOURCE_REVALIDATION, "source_revalidation_required", ()
        if not {ContentRight.READ, _RIGHTS[binding.action]}.issubset(context.rights or ()):
            return PolicyOutcome.DENY, "content_right_missing", ()
        if context.dlp_applicable and not state.dlp_allowed:
            return PolicyOutcome.DENY, "dlp_not_allowed", ()
        if binding.action in {DataAction.EMBED, DataAction.SEND_TO_MODEL}:
            models = (
                [profile for profile in state.models if profile.id == binding.model.model_profile_id]
                if binding.model
                else []
            )
            if len(models) != 1 or binding.model is None:
                return PolicyOutcome.DENY, "model_not_approved", ()
            try:
                models[0].require_allowed(binding.model, context, binding.action, now=now)
            except ProtectionDeniedError:
                return PolicyOutcome.DENY, "model_not_approved", ()
        rules = tuple(
            rule
            for rule in state.rules
            if context.classification_id in rule.classifications
            and binding.action == rule.action
            and binding.destination == rule.destination
        )
        if not rules or len({rule.id for rule in rules}) != len(rules):
            return PolicyOutcome.DENY, "no_unambiguous_rule", rules
        if any(rule.outcome == PolicyOutcome.DENY for rule in rules):
            return PolicyOutcome.DENY, "explicit_deny", rules
        if any(not set(context.restrictions).issubset(rule.permitted_restrictions) for rule in rules):
            return PolicyOutcome.DENY, "restriction_unsupported", rules
        if (binding.destination in _STORES or _RIGHTS[binding.action] == ContentRight.PERSIST) and (
            not context.delete_with_source
            or context.retention_policy_ref is None
            or any(context.retention_policy_ref not in rule.retention_policy_refs for rule in rules)
        ):
            return PolicyOutcome.DENY, "retention_not_approved", rules
        for rule in rules:
            if rule.outcome != PolicyOutcome.ALLOW:
                return rule.outcome, "rule_requires_action", rules
        if (context.encrypted or context.protection_status == "protected") and not any(
            obligation.kind == ObligationKind.PROTECTED_DISPLAY
            for rule in rules
            for obligation in rule.obligations
        ):
            return PolicyOutcome.REQUIRE_PROTECTION, "protected_handler_required", rules
        if (context.encrypted or context.protection_status == "protected") and (
            binding.action != DataAction.DISPLAY or binding.destination != DataDestination.USER_SCREEN
        ):
            return PolicyOutcome.DENY, "protected_handler_unsupported", rules
        return PolicyOutcome.ALLOW, "explicitly_allowed", rules

    def evaluate(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyDecision:
        with self._lock:
            return self._evaluate(context, binding)

    def _evaluate(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyDecision:
        context, binding = ProtectionContext.model_validate(context), DataUseBinding.model_validate(binding)
        state = self._resolve(context, binding)
        now = self.clock()
        if not 0 <= now < float("inf"):
            raise ProtectionDeniedError("Policy clock is invalid.")
        self._issued = {key: value for key, value in self._issued.items() if now < value[0].revalidate_at}
        self._claimed.intersection_update(self._issued)
        outcome, reason, rules = self._assess(context, binding, state, now)
        obligations = tuple(dict.fromkeys(item for rule in rules for item in rule.obligations))
        if len({item.id for item in obligations}) != len(obligations):
            outcome, reason, obligations = PolicyOutcome.DENY, "obligation_conflict", ()
        if len(self._issued) >= 1024:
            outcome, reason = PolicyOutcome.DENY, "permit_capacity_exceeded"
        expiry = now + 30
        if outcome == PolicyOutcome.ALLOW and state is not None:
            expiry = min(
                expiry,
                state.revalidate_at,
                state.execution_deadline,
                *(item.snapshot.revalidate_at for item in state.sources),
                *(value for value in (context.expires_at, context.revalidate_at) if value is not None),
            )
        identifier = uuid4().hex
        decision = PolicyDecision(
            id=identifier,
            outcome=outcome,
            binding=binding,
            rule_refs=tuple(rule.id for rule in rules) or ("deny-default",),
            reason_codes=(reason,),
            explanation="Protection policy evaluated the exact bound data use.",
            obligations=obligations,
            policy_version=binding.policy_version,
            evaluated_at=now,
            revalidate_at=expiry,
            audit_ref=uuid4().hex,
        )
        try:
            self.audit.record(
                PolicyAudit(
                    decision_id=decision.id,
                    outcome=decision.outcome,
                    action=binding.action,
                    destination=binding.destination,
                    rule_refs=decision.rule_refs,
                    reason_codes=decision.reason_codes,
                    policy_version=decision.policy_version,
                    evaluated_at=now,
                    audit_ref=decision.audit_ref,
                )
            )
        except Exception as error:
            _failure("audit", error)
            raise ProtectionDeniedError(
                "Protection audit is unavailable; content was not released."
            ) from None
        if outcome == PolicyOutcome.ALLOW and state is not None:
            self._issued[decision.id] = (decision, state)
        return decision

    def _validate_decision(self, decision: PolicyDecision) -> None:
        entry = self._issued.get(decision.id)
        if entry is None or entry[0] != decision:
            raise ProtectionDeniedError("Protection decision is not current or was not issued here.")
        self._validate_state(decision, entry[1])

    def _validate_state(self, decision: PolicyDecision, issued_state: PolicyState) -> None:
        state = self._resolve(issued_state.context, decision.binding)
        now = self.clock()
        if (
            not decision.evaluated_at <= now < decision.revalidate_at
            or state != issued_state
            or self._assess(issued_state.context, decision.binding, state, now)[0] != PolicyOutcome.ALLOW
        ):
            raise ProtectionDeniedError("Protection policy or source authority changed.")

    def _validate_proof(self, proof: ObligationProof, decision: PolicyDecision) -> None:
        if self.verifier is None:
            raise ProtectionDeniedError("Protection obligation has no trusted verifier.")
        try:
            self.verifier.validate(proof, decision)
        except Exception as error:
            _failure("obligation", error)
            raise ProtectionDeniedError("Protection obligation could not be verified.") from None

    def permit(
        self,
        context: ProtectionContext,
        binding: DataUseBinding,
        *,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> PolicyPermit:
        decision = self.evaluate(context, binding)
        return self.authorize(decision, proofs=proofs)

    def authorize(
        self,
        decision: PolicyDecision,
        *,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> PolicyPermit:
        decision = PolicyDecision.model_validate(decision)
        with self._lock:
            if decision.id in self._claimed:
                raise ProtectionDeniedError("Protection decision already has a permit.")
            self._validate_decision(decision)
            self._claimed.add(decision.id)
            try:
                return PolicyPermit(
                    decision,
                    proofs=proofs,
                    authority=_PermitAuthority(self, self._issued[decision.id][1]),
                    now=self.clock(),
                )
            except Exception:
                self.revoke(decision.id)
                raise

    def revoke(self, identifier: str) -> None:
        with self._lock:
            self._issued.pop(identifier, None)
            self._claimed.discard(identifier)


class _PermitAuthority:
    def __init__(self, policy: ProtectionPolicy, state: PolicyState) -> None:
        self.policy = policy
        self.state = state
        self.initial = True

    def validate_decision(self, decision: PolicyDecision) -> None:
        with self.policy._lock:
            if self.initial:
                self.initial = False
                self.policy._validate_decision(decision)
            else:
                try:
                    self.policy._validate_decision(decision)
                finally:
                    self.policy.revoke(decision.id)

    def validate_proof(self, proof: ObligationProof, decision: PolicyDecision) -> None:
        self.policy._validate_proof(proof, decision)
        self.policy._validate_state(decision, self.state)
