import json
from types import SimpleNamespace
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile

import pytest
from pydantic import ValidationError
from scripts import enterprise_acceptance as runner
from scripts.smoke_enterprise_wheel import verify_wheel

from shovel.capability.domain import CapabilityScope, Permission
from shovel.enterprise.acceptance import (
    LIVE_CHECKS,
    REQUIRED_TESTS,
    AcceptanceUnavailableError,
    CanaryUse,
    CandidateArtifact,
    LiveVerification,
    OfflineEvidence,
    RolloutPlan,
    RolloutReviewer,
    collect_test_evidence,
)
from shovel.protection.domain import DataAction, DataDestination

COMMIT = "a" * 40
ARTIFACTS = (
    CandidateArtifact(kind="python_wheel", sha256="b" * 64, size=100),
    CandidateArtifact(kind="native_bundle", sha256="c" * 64, size=200),
)


def plan():
    return RolloutPlan(
        scope=CapabilityScope(workspace_id="test-workspace", user_id="test-user"),
        source_commit=COMMIT,
        artifacts=ARTIFACTS,
        installation_digest="d" * 64,
        organization_policy_digest="e" * 64,
        governance_policy_digest="f" * 64,
        provider_manifest_digest="1" * 64,
        connection_binding_digest="2" * 64,
        connection_id="approved-connection",
        permissions=(Permission(action="enterprise.read", resource="approved-connection"),),
        data_uses=(CanaryUse(action=DataAction.READ, destination=DataDestination.WORKFLOW_STATE),),
    )


def evidence():
    return tuple(
        OfflineEvidence(
            lane=lane,
            source_commit=COMMIT,
            source_tree_clean=True,
            artifacts=ARTIFACTS,
            checks=dict.fromkeys(controls, "passed"),
            tests=10,
            failures=0,
        )
        for lane, controls in REQUIRED_TESTS.items()
    )


def junit(lane="python_contracts"):
    root = Element("testsuites")
    suite = SubElement(root, "testsuite")
    for names in REQUIRED_TESTS[lane].values():
        for name in names:
            SubElement(suite, "testcase", name=name)
    return root, suite


@pytest.mark.parametrize("outcome", ["passed", "skipped", "failed", "missing"])
def test_missing_skipped_and_failed_controls_are_not_acceptance(outcome):
    root, suite = junit()
    case = suite[0]
    if outcome == "missing":
        suite.remove(case)
    elif outcome != "passed":
        SubElement(case, "skipped" if outcome == "skipped" else "failure").text = "SECRET-FAILURE-BODY"
    result = collect_test_evidence(
        tostring(root), lane="python_contracts", source_commit=COMMIT, source_tree_clean=True
    )
    assert result.passed is (outcome == "passed")
    assert result.checks["restart"] == outcome
    assert "SECRET-FAILURE-BODY" not in result.model_dump_json()
    assert not result.production_ready and not result.live_acceptance_complete


def test_parameter_failure_unknown_failure_and_xml_payloads_cannot_hide():
    root, suite = junit()
    case = SubElement(suite, "testcase", name=suite[0].get("name") + "[other]")
    SubElement(case, "failure").text = "TOKEN-AND-EXCEPTION-CONTENTS"
    result = collect_test_evidence(
        tostring(root), lane="python_contracts", source_commit=COMMIT, source_tree_clean=True
    )
    assert not result.passed and result.checks["restart"] == "failed"
    suite.remove(case)
    SubElement(SubElement(suite, "testcase", name="UNTRUSTED-NAME"), "error").text = "RAW-SECRET"
    result = collect_test_evidence(
        tostring(root), lane="python_contracts", source_commit=COMMIT, source_tree_clean=True
    )
    assert not result.passed and "UNTRUSTED-NAME" not in result.model_dump_json()
    assert "RAW-SECRET" not in result.model_dump_json()
    with pytest.raises(ValueError):
        collect_test_evidence(
            b'<!DOCTYPE x [<!ENTITY x "sensitive">]><testsuites/>',
            lane="python_contracts",
            source_commit=COMMIT,
            source_tree_clean=True,
        )


def test_trx_and_absent_cases_are_reported_without_raw_names_or_tokens():
    root = Element("TestRun")
    for names in REQUIRED_TESTS["native_contracts"].values():
        for name in names:
            SubElement(root, "UnitTestResult", testName=name, outcome="Passed")
    result = collect_test_evidence(
        tostring(root), lane="native_contracts", source_commit=COMMIT, source_tree_clean=True
    )
    assert result.passed
    SubElement(root, "RunInfo").text = "SECRET-NATIVE-ERROR"
    failed = collect_test_evidence(
        tostring(root), lane="native_contracts", source_commit=COMMIT, source_tree_clean=True
    )
    assert not failed.passed and "SECRET-NATIVE-ERROR" not in failed.model_dump_json()


@pytest.mark.parametrize(
    "raw",
    [b"<broken", "<!DOCTYPE x><x/>".encode("utf-16"), b"x" * (8 * 1024 * 1024 + 1)],
    ids=["malformed", "utf16", "oversized"],
)
def test_malformed_non_utf8_and_oversized_xml_cannot_produce_a_receipt(raw):
    with pytest.raises(ValueError):
        collect_test_evidence(raw, lane="python_contracts", source_commit=COMMIT, source_tree_clean=True)


@pytest.mark.parametrize(
    "change",
    [
        {"permissions": (Permission(action="enterprise.write", resource="approved-connection"),)},
        {"permissions": (Permission(action="enterprise.read", resource="*"),)},
        {
            "permissions": (
                Permission(action="enterprise.read", resource="approved-connection"),
                Permission(action="enterprise.read", resource="another"),
            )
        },
        {"function_id": "arbitrary.query"},
        {"artifacts": (ARTIFACTS[0], ARTIFACTS[0])},
        {"data_uses": (CanaryUse(action=DataAction.READ, destination=DataDestination.EXTERNAL_MODEL),)},
        {"data_uses": (CanaryUse(action=DataAction.PERSIST, destination=DataDestination.LOCAL_SQLITE),)},
    ],
)
def test_canary_proposal_cannot_broaden_permissions_functions_or_artifacts(change):
    with pytest.raises(ValidationError):
        RolloutPlan.model_validate({**plan().model_dump(), **change})


@pytest.mark.parametrize(
    "change",
    [{"source_commit": "0" * 40}, {"source_tree_clean": False}, {"artifacts": ()}, {"failures": 1}],
)
def test_green_tests_cannot_hide_source_or_artifact_mismatch(change):
    values = list(evidence())
    values[0] = values[0].model_copy(update=change)
    result = RolloutReviewer().review(plan(), tuple(values))
    assert result.status == "blocked" and result.reason == "offline_evidence_incomplete"
    assert result.permissions_applied == ()


def test_offline_proof_and_json_flags_never_open_production():
    result = RolloutReviewer().review(plan(), evidence())
    assert result.reason == "live_verifier_unavailable"
    assert not result.production_ready and not result.permissions_applied
    for change in (
        {"production_ready": True},
        {"live_acceptance_complete": True},
        {"checks": {"ALLOW": "passed"}},
    ):
        with pytest.raises(ValidationError):
            OfflineEvidence.model_validate({**evidence()[0].model_dump(), **change})


def test_wrong_lane_artifact_and_missing_lane_do_not_qualify():
    values = list(evidence())
    values[2] = values[2].model_copy(update={"artifacts": (ARTIFACTS[0],)})
    assert RolloutReviewer().review(plan(), tuple(values)).reason == "offline_evidence_incomplete"
    assert RolloutReviewer().review(plan(), evidence()[:-1]).reason == "offline_evidence_incomplete"


def authority(alter=lambda proof: proof):
    def verify(request):
        return alter(
            LiveVerification(
                nonce=request.nonce,
                request_digest=request.request_digest,
                observed_at=1000,
                expires_at=1010,
                state="current",
                verified_checks=LIVE_CHECKS,
            )
        )

    return SimpleNamespace(verify=verify)


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "0" * 64},
        {"request_digest": "0" * 64},
        {"state": "revoked"},
        {"expires_at": 1000},
        {"expires_at": 1031},
        {"observed_at": 999},
        {"verified_checks": LIVE_CHECKS[:-1]},
    ],
)
def test_replayed_forged_stale_or_partial_live_evidence_is_blocked(change):
    reviewer = RolloutReviewer(authority(lambda proof: proof.model_copy(update=change)), clock=lambda: 1000)
    result = reviewer.review(plan(), evidence())
    assert result.reason == "live_evidence_invalid" and not result.permissions_applied


def test_independent_current_proof_only_qualifies_for_review_and_old_proof_is_not_reusable():
    seen = []

    def capture(proof):
        seen.append(proof)
        return proof

    reviewer = RolloutReviewer(authority(capture), clock=lambda: 1000)
    result = reviewer.review(plan(), evidence())
    assert result.status == "eligible_for_operator_review"
    assert not result.production_ready and not result.permissions_applied
    replay = RolloutReviewer(authority(lambda _: seen[0]), clock=lambda: 1000)
    assert replay.review(plan(), evidence()).status == "blocked"
    assert RolloutReviewer().review(plan(), evidence()).status == "blocked"

    def unavailable(_request):
        raise AcceptanceUnavailableError("RAW-PROVIDER-ERROR")

    result = RolloutReviewer(SimpleNamespace(verify=unavailable)).review(plan(), evidence())
    assert (
        result.reason == "live_verifier_unavailable" and "RAW-PROVIDER-ERROR" not in result.model_dump_json()
    )


def test_cli_collects_sanitized_evidence_and_never_overwrites_or_opens(tmp_path, monkeypatch, capsys):
    report, output = tmp_path / "report.xml", tmp_path / "safe.json"
    report.write_bytes(tostring(junit()[0]))
    monkeypatch.setattr(runner, "source_identity", lambda: (COMMIT, True))
    monkeypatch.setattr(
        "sys.argv",
        [
            "enterprise_acceptance",
            "collect",
            "--lane",
            "python_contracts",
            "--report",
            str(report),
            "--output",
            str(output),
        ],
    )
    assert runner.main() == 0
    saved = output.read_bytes()
    assert not json.loads(saved)["production_ready"]
    assert runner.main() == 2 and output.read_bytes() == saved
    assert str(tmp_path) not in capsys.readouterr().out


def test_wheel_probe_binds_actual_installed_inventory_and_bytes(tmp_path):
    package = tmp_path / "site-packages" / "shovel"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_bytes(b"wheel bytes")
    wheel = tmp_path / "candidate.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("shovel/__init__.py", b"wheel bytes")
    verify_wheel(wheel, package)
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        verify_wheel(wheel, package)
    source.write_bytes(b"wheel bytes")
    (package / "unlisted.py").write_bytes(b"extra")
    with pytest.raises(ValueError):
        verify_wheel(wheel, package)


def test_cli_green_receipts_still_require_independent_verifier(tmp_path, monkeypatch, capsys):
    proposal, output = tmp_path / "proposal.json", tmp_path / "review.json"
    proposal.write_text(plan().model_dump_json(), encoding="utf-8")
    arguments = ["enterprise_acceptance", "review", "--plan", str(proposal), "--output", str(output)]
    for index, value in enumerate(evidence()):
        path = tmp_path / f"lane-{index}.json"
        path.write_text(value.model_dump_json(), encoding="utf-8")
        arguments.extend(("--evidence", str(path)))
    monkeypatch.setattr("sys.argv", arguments)
    assert runner.main() == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["reason"] == "live_verifier_unavailable"
    assert result["permissions_applied"] == [] and not result["production_ready"]
    assert str(tmp_path) not in capsys.readouterr().out


def test_cli_invalid_arguments_do_not_echo_secret_or_path(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["enterprise_acceptance", "collect", "--lane", "SYNTHETIC-SECRET"])
    with pytest.raises(SystemExit) as exited:
        runner.main()
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "SYNTHETIC-SECRET" not in captured.out + captured.err
