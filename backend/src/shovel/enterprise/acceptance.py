"""Candidate qualification, not an enterprise authorization or installation API."""

from __future__ import annotations

import secrets
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Annotated, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from shovel.capability.domain import CapabilityScope, Digest, Identifier, Permission
from shovel.identity.domain import UnixSeconds
from shovel.protection.domain import DataAction, DataDestination, ProtectionValue, content_digest

Lane = Literal["python_contracts", "windows_ipc", "native_contracts", "installed_wheel"]
Result = Literal["passed", "failed", "skipped", "missing"]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]

# Only fixed control IDs enter published receipts; test output and names never do.
REQUIRED_TESTS: dict[Lane, dict[str, tuple[str, ...]]] = {
    "python_contracts": {
        "restart": ("test_authentication_suspend_is_atomic_restartable_and_explicitly_resumed",),
        "concurrency": ("test_concurrent_manual_resume_and_dispatch_release_only_one_result",),
        "revocation": ("test_source_revocation_fences_every_release_and_late_result",),
        "credential_containment": (
            "test_sdk_and_content_sentinels_do_not_enter_diagnostics_reprs_or_sqlite",
        ),
        "effect_recovery": ("test_crash_windows_reconcile_then_explicit_resume_without_duplicate",),
        "storage_recovery": ("test_old_backup_cannot_discard_a_later_uncertain_external_effect",),
        "default_deny": ("test_default_production_policy_still_denies_connection_backed_invocation",),
        "outlet_containment": (
            "test_read_never_grants_a_raw_or_derived_legacy_exit",
            "test_actual_chat_never_streams_or_persists_unapproved_enterprise_body",
            "test_actual_legacy_sinks_deny_before_io_or_enqueue",
        ),
        "adapter_boundary": (
            "test_fixed_kusto_metadata_http_no_redirect_or_business_query",
            "test_signature_pins_expiry_duplicate_fields_and_rights_fail_closed",
        ),
    },
    "windows_ipc": {
        "private_channel": ("test_real_private_pipes_reach_only_ephemeral_context",),
        "forgery": ("test_real_private_pipes_reject_forged_receipts_and_credentials",),
        "replay": ("test_previous_child_response_cannot_be_replayed_in_a_new_acquisition",),
        "cancellation": ("test_cancel_delivers_bound_control_and_discards_late_private_credential",),
        "lease_concurrency": ("test_concurrent_acquisition_consumes_one_lease_and_restart_cannot_reuse_it",),
    },
    "native_contracts": {
        "install_signature": ("SignedEnvelopeRejectsWrongSignerTamperAndRollback",),
        "caller_binding": ("SignedPythonAloneIsNotAnAuthorizedCaller",),
        "private_handles": ("InheritedHandlesCannotLeakToFurtherChildren",),
        "one_use_credential": ("CredentialUsesBoundBinaryChannelAndCannotSerializeOrReuse",),
        "unsafe_frames": ("InvalidFrameAndUserWritableInstallationAreRejected",),
    },
    "installed_wheel": {
        "packaged_runtime": ("packaged_runtime",),
        "packaged_enterprise": ("packaged_enterprise",),
        "default_deny": ("default_deny",),
    },
}

LIVE_CHECKS = (
    "signed_installation",
    "ci_provenance",
    "organization_approval",
    "wam_account_resource_and_cancel",
    "real_metadata_adapter",
    "restart_and_control_recovery",
    "concurrent_single_release",
    "inflight_revocation",
    "installed_ipc_replay_and_forgery",
    "credential_containment",
    "unsupported_outlets_denied",
    "storage_key_and_backup_lifecycle",
)


class CandidateArtifact(ProtectionValue):
    kind: Literal["python_wheel", "native_bundle"]
    sha256: Digest
    size: int = Field(gt=0, strict=True)


class OfflineEvidence(ProtectionValue):
    purpose: Literal["shovel.enterprise.offline-acceptance.v1"] = "shovel.enterprise.offline-acceptance.v1"
    lane: Lane
    source_commit: Commit
    source_tree_clean: bool = Field(strict=True)
    artifacts: tuple[CandidateArtifact, ...] = Field(max_length=2)
    checks: dict[str, Result]
    tests: int = Field(ge=0, strict=True)
    failures: int = Field(ge=0, strict=True)
    production_ready: Literal[False] = False
    live_acceptance_complete: Literal[False] = False
    trusted_installation_verified: Literal[False] = False

    @model_validator(mode="after")
    def fixed_controls(self) -> OfflineEvidence:
        if set(self.checks) != set(REQUIRED_TESTS[self.lane]):
            raise ValueError("Evidence must contain exactly the lane's fixed controls.")
        if len({artifact.kind for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("Artifact kinds must be unique.")
        return self

    @property
    def passed(self) -> bool:
        return self.tests > 0 and self.failures == 0 and all(v == "passed" for v in self.checks.values())


def collect_test_evidence(
    raw: bytes,
    *,
    lane: Lane,
    source_commit: str,
    source_tree_clean: bool,
    artifacts: tuple[CandidateArtifact, ...] = (),
) -> OfflineEvidence:
    """Reduce untrusted JUnit/TRX to fixed statuses; raw XML is never published."""
    if lane == "installed_wheel":
        raise ValueError("Installed-wheel evidence requires the actual package probe.")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Unsupported test report.")
    text = raw.decode("utf-8-sig")
    if "\0" in text or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("Unsupported test report.")
    try:
        root = ET.fromstring(text)  # noqa: S314 -- UTF-8 only, bounded, DTD/entities rejected above
    except ET.ParseError:
        raise ValueError("Invalid test report.") from None
    outcomes: dict[str, list[Result]] = {}
    tests = failures = 0
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("testcase", "UnitTestResult"):
            continue
        tests += 1
        if tag == "testcase":
            children = {child.tag.rsplit("}", 1)[-1] for child in item}
            status: Result = (
                "failed"
                if children & {"failure", "error"}
                else "skipped"
                if "skipped" in children
                else "passed"
            )
            name = item.get("name", "").split("[", 1)[0]
        else:
            status = (
                "passed"
                if item.get("outcome") == "Passed"
                else "skipped"
                if item.get("outcome") == "NotExecuted"
                else "failed"
            )
            name = item.get("testName", "")
        failures += status == "failed"
        outcomes.setdefault(name, []).append(status)
    # A runner/setup error outside a testcase cannot disappear in the reduction.
    if any(item.tag.rsplit("}", 1)[-1] in ("failure", "error", "RunInfo") for item in root.iter()):
        failures = max(1, failures)
    checks: dict[str, Result] = {}
    for control, names in REQUIRED_TESTS[lane].items():
        values = [value for name in names for value in outcomes.get(name, ["missing"])]
        checks[control] = next(
            (status for status in ("failed", "missing", "skipped") if status in values), "passed"
        )
    return OfflineEvidence(
        lane=lane,
        source_commit=source_commit,
        source_tree_clean=source_tree_clean,
        artifacts=artifacts,
        checks=checks,
        tests=tests,
        failures=failures,
    )


class CanaryUse(ProtectionValue):
    action: DataAction
    destination: DataDestination


class RolloutPlan(ProtectionValue):
    """The first supported proposal: one scoped metadata-read connection only."""

    scope: CapabilityScope
    source_commit: Commit
    artifacts: tuple[CandidateArtifact, ...] = Field(min_length=2, max_length=2)
    installation_digest: Digest
    organization_policy_digest: Digest
    governance_policy_digest: Digest
    provider_manifest_digest: Digest
    connection_binding_digest: Digest
    connection_id: Identifier
    function_id: Literal["kusto.metadata.read"] = "kusto.metadata.read"
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=1)
    data_uses: tuple[CanaryUse, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def least_privilege(self) -> RolloutPlan:
        if {artifact.kind for artifact in self.artifacts} != {"python_wheel", "native_bundle"}:
            raise ValueError("A proposal must bind both candidate artifacts.")
        if self.permissions != (Permission(action="enterprise.read", resource=self.connection_id),):
            raise ValueError("Only one exact connection read permission is supported.")
        uses = {(use.action, use.destination) for use in self.data_uses}
        read = DataAction.READ, DataDestination.WORKFLOW_STATE
        display = DataAction.DISPLAY, DataDestination.USER_SCREEN
        if read not in uses or not uses <= {read, display} or len(uses) != len(self.data_uses):
            raise ValueError("Only explicit metadata READ and optional screen DISPLAY are supported.")
        return self


class LiveVerificationRequest(ProtectionValue):
    nonce: Digest
    request_digest: Digest
    plan: RolloutPlan
    evidence_digests: tuple[Digest, ...]
    issued_at: UnixSeconds


class LiveVerification(ProtectionValue):
    """Output of an independently installed verifier, never a user/CI JSON assertion."""

    nonce: Digest
    request_digest: Digest
    observed_at: UnixSeconds
    expires_at: UnixSeconds
    state: Literal["current", "revoked"]
    verified_checks: tuple[str, ...]


class AcceptanceUnavailableError(RuntimeError):
    pass


class LiveAcceptanceAuthority(Protocol):
    def verify(self, request: LiveVerificationRequest) -> LiveVerification:
        """Verify current organization approval, provenance and real installed tests independently."""
        ...


class RolloutReview(ProtectionValue):
    status: Literal["blocked", "eligible_for_operator_review"]
    reason: Literal[
        "offline_evidence_incomplete", "live_verifier_unavailable", "live_evidence_invalid", "review_only"
    ]
    production_ready: Literal[False] = False
    permissions_applied: tuple[Permission, ...] = Field(default=(), max_length=0)


class RolloutReviewer:
    """No registration/default authority, runtime grants, policy writes or enable side effects."""

    def __init__(
        self, authority: LiveAcceptanceAuthority | None = None, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.authority, self.clock = authority, clock

    def review(self, plan: RolloutPlan, evidence: tuple[OfflineEvidence, ...]) -> RolloutReview:
        plan = RolloutPlan.model_validate(plan)
        evidence = tuple(OfflineEvidence.model_validate(item) for item in evidence)
        expected = {artifact.kind: artifact for artifact in plan.artifacts}
        lane_artifact = {
            "python_contracts": "python_wheel",
            "installed_wheel": "python_wheel",
            "windows_ipc": "native_bundle",
            "native_contracts": "native_bundle",
        }
        if (
            {item.lane for item in evidence} != set(REQUIRED_TESTS)
            or len(evidence) != len(REQUIRED_TESTS)
            or any(
                not item.passed
                or not item.source_tree_clean
                or item.source_commit != plan.source_commit
                or lane_artifact[item.lane] not in {artifact.kind for artifact in item.artifacts}
                or any(expected[artifact.kind] != artifact for artifact in item.artifacts)
                for item in evidence
            )
        ):
            return RolloutReview(status="blocked", reason="offline_evidence_incomplete")
        if self.authority is None:
            return RolloutReview(status="blocked", reason="live_verifier_unavailable")
        digests = tuple(
            content_digest(item.model_dump(mode="json"))
            for item in sorted(evidence, key=lambda item: item.lane)
        )
        nonce = secrets.token_hex(32)
        issued_at = self.clock()
        request = LiveVerificationRequest(
            nonce=nonce,
            request_digest=content_digest(
                {"plan": plan.model_dump(mode="json"), "evidence": digests, "nonce": nonce}
            ),
            plan=plan,
            evidence_digests=digests,
            issued_at=issued_at,
        )
        try:
            proof = LiveVerification.model_validate(self.authority.verify(request))
        except AcceptanceUnavailableError:
            return RolloutReview(status="blocked", reason="live_verifier_unavailable")
        except ValidationError:
            return RolloutReview(status="blocked", reason="live_evidence_invalid")
        now = self.clock()
        if (
            proof.nonce != request.nonce
            or proof.request_digest != request.request_digest
            or proof.state != "current"
            or not issued_at <= proof.observed_at <= now
            or not now < proof.expires_at <= issued_at + 30
            or len(proof.verified_checks) != len(LIVE_CHECKS)
            or set(proof.verified_checks) != set(LIVE_CHECKS)
        ):
            return RolloutReview(status="blocked", reason="live_evidence_invalid")
        return RolloutReview(status="eligible_for_operator_review", reason="review_only")
