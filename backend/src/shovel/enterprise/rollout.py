"""Installed live-evidence verification and a narrowing, default-deny Runtime gate."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier
from shovel.enterprise.acceptance import (
    LIVE_CHECKS,
    AcceptanceUnavailableError,
    LiveVerification,
    LiveVerificationRequest,
    OfflineEvidence,
    RolloutPlan,
    RolloutReviewer,
)
from shovel.enterprise.organization import OrganizationAnchor, verify_signed_document
from shovel.identity.domain import UnixSeconds
from shovel.protection.domain import (
    DataAction,
    DataDestination,
    ProtectionDeniedError,
    ProtectionValue,
    content_digest,
)


class LiveMeasurement(ProtectionValue):
    check: str
    environment: Literal["installed_live"]
    result: Literal["passed", "failed", "skipped"]
    receipt_digest: Digest


class LiveReport(ProtectionValue):
    purpose: Literal["shovel.enterprise.live-report.v1"]
    organization_id: Identifier
    revision: int = Field(strict=True, ge=1)
    plan_digest: Digest
    evidence_digests: tuple[Digest, ...]
    observed_at: UnixSeconds
    expires_at: UnixSeconds
    measurements: tuple[LiveMeasurement, ...] = Field(max_length=len(LIVE_CHECKS))


class RolloutApproval(ProtectionValue):
    purpose: Literal["shovel.enterprise.rollout-approval.v1"]
    organization_id: Identifier
    revision: int = Field(strict=True, ge=1)
    issued_at: UnixSeconds
    expires_at: UnixSeconds
    plan: RolloutPlan
    evidence: tuple[OfflineEvidence, ...] = Field(min_length=4, max_length=4)
    report_digest: Digest


class RolloutState(ProtectionValue):
    schema_version: Literal[1] = Field(alias="schema")
    organization_id: Identifier = Field(alias="organizationId")
    approval_signer: Digest = Field(alias="approvalSigner")
    verifier_signer: Digest = Field(alias="verifierSigner")
    generation: int = Field(strict=True, ge=1)
    highest_revision: int = Field(strict=True, ge=0, alias="highestRevision")
    status: Literal["unapproved", "approved", "enabled", "paused", "revoked"]
    approval_digest: Digest | None = Field(default=None, alias="approvalDigest")
    report_digest: Digest | None = Field(default=None, alias="reportDigest")
    enabled_at: UnixSeconds | None = Field(default=None, alias="enabledAt")
    revoked: tuple[Digest, ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def consistent(self) -> RolloutState:
        if (
            self.approval_signer == self.verifier_signer
            or len(set(self.revoked)) != len(self.revoked)
            or (
                self.status == "unapproved"
                and (self.highest_revision != 0 or self.approval_digest is not None)
            )
            or (
                self.status != "unapproved"
                and (self.highest_revision < 1 or self.approval_digest is None or self.report_digest is None)
            )
            or (self.status == "enabled" and self.enabled_at is None)
            or (self.status in {"approved", "enabled", "paused"} and self.approval_digest in self.revoked)
        ):
            raise ValueError("Inconsistent rollout machine state.")
        return self


@dataclass(frozen=True)
class RolloutMaterial:
    state: RolloutState
    approval: bytes
    report: bytes
    installation_digest: str


type RolloutLoader = Callable[[], RolloutMaterial | None]
type ControlLease = Callable[[], AbstractContextManager[None]]


class SignedLiveAcceptanceAuthority:
    """Verify independent live receipts and separate organization approval, not CI/user flags."""

    def __init__(self, loader: RolloutLoader, *, clock: Callable[[], float] = time.time) -> None:
        self.loader, self.clock = loader, clock

    def current(self) -> tuple[RolloutMaterial, RolloutApproval, LiveReport]:
        try:
            material = self.loader()
            if material is None:
                raise ProtectionDeniedError("Live acceptance is not installed.")
            state = RolloutState.model_validate(material.state.model_dump(mode="json", by_alias=True))
            if (
                state.status not in {"approved", "enabled", "paused"}
                or state.approval_digest is None
                or state.report_digest is None
            ):
                raise ProtectionDeniedError("Live acceptance is not approved.")
            now = self.clock()
            approval_document, _, approval_certificate_expiry = verify_signed_document(
                material.approval,
                OrganizationAnchor(
                    organization_id=state.organization_id,
                    revision=state.highest_revision,
                    certificate_sha256=state.approval_signer,
                    envelope_sha256=state.approval_digest,
                ),
                now=now,
            )
            approval = RolloutApproval.model_validate(approval_document)
            report_document, _, report_certificate_expiry = verify_signed_document(
                material.report,
                OrganizationAnchor(
                    organization_id=state.organization_id,
                    revision=state.highest_revision,
                    certificate_sha256=state.verifier_signer,
                    envelope_sha256=state.report_digest,
                ),
                now=now,
            )
            report = LiveReport.model_validate(report_document)
            digests = tuple(
                content_digest(item.model_dump(mode="json"))
                for item in sorted(approval.evidence, key=lambda item: item.lane)
            )
            if (
                approval.organization_id != state.organization_id
                or report.organization_id != state.organization_id
                or approval.revision != state.highest_revision
                or report.revision != approval.revision
                or not report.observed_at <= approval.issued_at <= now < approval.expires_at
                or not now < report.expires_at <= report_certificate_expiry
                or approval.expires_at > min(report.expires_at, approval_certificate_expiry)
                or approval.report_digest != state.report_digest
                or report.plan_digest != content_digest(approval.plan.model_dump(mode="json"))
                or report.evidence_digests != digests
                or approval.plan.installation_digest != material.installation_digest
                or len(report.measurements) != len(LIVE_CHECKS)
                or {item.check for item in report.measurements} != set(LIVE_CHECKS)
                or any(item.result != "passed" for item in report.measurements)
                or RolloutReviewer().review(approval.plan, approval.evidence).reason
                != "live_verifier_unavailable"
            ):
                raise ProtectionDeniedError("Live acceptance bindings or measurements are invalid.")
            if self.loader() != material or self.clock() >= min(
                approval.expires_at, report.expires_at, approval_certificate_expiry, report_certificate_expiry
            ):
                raise ProtectionDeniedError("Live acceptance changed during verification.")
            return material, approval, report
        except OSError, ValueError, TypeError, KeyError:
            raise AcceptanceUnavailableError("Installed live acceptance is unavailable.") from None

    def verify(self, request: LiveVerificationRequest) -> LiveVerification:
        request = LiveVerificationRequest.model_validate(request)
        material, approval, report = self.current()
        now = self.clock()
        expected = content_digest(
            {
                "plan": request.plan.model_dump(mode="json"),
                "evidence": request.evidence_digests,
                "nonce": request.nonce,
            }
        )
        if (
            request.plan != approval.plan
            or request.evidence_digests != report.evidence_digests
            or request.request_digest != expected
            or not now - 30 <= request.issued_at <= now
        ):
            raise AcceptanceUnavailableError("Live verification request does not match approval.")
        return LiveVerification(
            nonce=request.nonce,
            request_digest=request.request_digest,
            observed_at=now,
            expires_at=min(now + 5, request.issued_at + 30, approval.expires_at, report.expires_at),
            state="current" if material.state.status in {"approved", "enabled", "paused"} else "revoked",
            verified_checks=LIVE_CHECKS,
        )


class RolloutGate:
    def __init__(
        self,
        authority: SignedLiveAcceptanceAuthority | None = None,
        lease: ControlLease | None = None,
    ) -> None:
        self.authority, self.lease = authority, lease

    def require(
        self,
        *,
        scope: CapabilityScope,
        organization_id: str,
        connection_id: str,
        organization_policy_digest: str,
        governance_policy_digest: str,
        provider_manifest_digest: str,
        connection_binding_digest: str,
        execution_created_at: float,
        action: DataAction = DataAction.READ,
        destination: DataDestination = DataDestination.WORKFLOW_STATE,
        expected: str | None = None,
    ) -> str:
        if self.authority is None or self.lease is None:
            raise ProtectionDeniedError("Enterprise rollout has not been installed and enabled.")
        try:
            material, approval, report = self.authority.current()
            plan = approval.plan
            binding = content_digest(
                {
                    "generation": material.state.generation,
                    "approval": material.state.approval_digest,
                    "plan": report.plan_digest,
                }
            )
            if (
                material.state.status != "enabled"
                or material.state.enabled_at is None
                or not material.state.enabled_at <= execution_created_at <= self.authority.clock()
                or plan.scope != scope
                or approval.organization_id != organization_id
                or plan.connection_id != connection_id
                or plan.organization_policy_digest != organization_policy_digest
                or plan.governance_policy_digest != governance_policy_digest
                or plan.provider_manifest_digest != provider_manifest_digest
                or plan.connection_binding_digest != connection_binding_digest
                or (action, destination) not in {(use.action, use.destination) for use in plan.data_uses}
                or (expected is not None and expected != binding)
            ):
                raise ProtectionDeniedError("Enterprise rollout is inactive, changed or out of scope.")
            return binding
        except AcceptanceUnavailableError:
            raise ProtectionDeniedError("Current live acceptance verification is unavailable.") from None

    @contextmanager
    def release(self) -> Iterator[None]:
        if self.lease is None:
            raise ProtectionDeniedError("Enterprise rollout release lease is unavailable.")
        with self.lease():
            yield


_installed: RolloutGate | None = None


def configure_rollout_host(loader: RolloutLoader, lease: ControlLease) -> None:
    import sys

    from shovel.identity.native_wam import installed_host

    global _installed
    if _installed is not None or not installed_host() or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Protected installed bootstrap is required for enterprise rollout.")
    _installed = RolloutGate(SignedLiveAcceptanceAuthority(loader), lease)


def installed_rollout() -> RolloutGate:
    return _installed if _installed is not None else RolloutGate()
