from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shovel.protection.domain import (
        DataUseBinding,
        Lineage,
        ObligationProof,
        PolicyDecision,
        ProtectionContext,
    )


class PolicyAuthority(Protocol):
    """Runtime-owned authority must validate issuance, freshness and revocation."""

    def validate_decision(self, decision: PolicyDecision) -> None: ...
    def validate_proof(self, proof: ObligationProof, decision: PolicyDecision) -> None: ...


class ProtectionMergePolicy(Protocol):
    def merge(self, contexts: tuple[ProtectionContext, ...], lineage: Lineage) -> ProtectionContext: ...


class DataProtectionPolicy(Protocol):
    def evaluate(self, context: ProtectionContext, binding: DataUseBinding) -> PolicyDecision: ...
