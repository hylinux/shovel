"""Offline qualification runner. Never logs in, installs software, or changes grants."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Never

from shovel.enterprise.acceptance import (
    CandidateArtifact,
    Lane,
    OfflineEvidence,
    RolloutPlan,
    RolloutReview,
    RolloutReviewer,
    collect_test_evidence,
)
from shovel.enterprise.organization import unique_json

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "test_enterprise_execution.py",
    "test_enterprise_failures.py",
    "test_external_effects.py",
    "test_enterprise_governance.py",
    "test_enterprise_egress.py",
    "test_organization_security.py",
    "test_native_wam.py",
    "test_enterprise_acceptance.py",
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        self.exit(
            2,
            '{"scope":"enterprise_offline_qualification","status":"failed",'
            '"reason":"invalid_arguments","production_ready":false}\n',
        )


def source_identity() -> tuple[str, bool]:
    git = shutil.which("git")
    if git is None:
        raise ValueError("Git is required for source identity.")
    commit = (
        subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=True, timeout=30
        )
        .stdout.decode("ascii")
        .strip()
    )
    dirty = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    return commit, not dirty


def artifact(value: str) -> CandidateArtifact:
    kind, separator, filename = value.partition("=")
    if not separator or kind not in ("python_wheel", "native_bundle"):
        raise ValueError("Unsupported artifact argument.")
    path = Path(filename)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return CandidateArtifact(kind=kind, sha256=digest, size=path.stat().st_size)


def limited_read(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Input exceeds the qualification limit.")
    return data


def collect(path: Path, lane: Lane, artifacts: tuple[CandidateArtifact, ...]) -> OfflineEvidence:
    commit, clean = source_identity()
    return collect_test_evidence(
        limited_read(path), lane=lane, source_commit=commit, source_tree_clean=clean, artifacts=artifacts
    )


def run(lane: Lane, artifacts: tuple[CandidateArtifact, ...]) -> OfflineEvidence:
    if lane not in ("python_contracts", "windows_ipc"):
        raise ValueError("The offline runner supports Python contract lanes only.")
    with TemporaryDirectory(prefix="shovel-acceptance-") as directory:
        report = Path(directory) / "tests.xml"
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("SHOVEL_", "PYTEST_", "PYTHON"))
        }
        files = ("test_native_wam.py",) if lane == "windows_ipc" else SUITES
        outcome = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "pytest",
                "--no-cov",
                "--junitxml",
                str(report),
                *(str(ROOT / "backend" / "tests" / filename) for filename in files),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
            check=False,
        )
        if not report.exists():
            raise ValueError("The offline test runner produced no report.")
        evidence = collect(report, lane, artifacts)
        if outcome.returncode and evidence.failures == 0:
            evidence = evidence.model_copy(update={"failures": 1})
        return evidence


def main() -> int:
    parser = SafeArgumentParser(prog="enterprise-acceptance", description=__doc__)
    parser.add_argument("command", choices=("run", "collect", "review"))
    parser.add_argument("--lane", choices=("python_contracts", "windows_ipc", "native_contracts"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result: OfflineEvidence | RolloutReview
        artifacts = tuple(artifact(value) for value in args.artifact)
        if args.command == "review":
            if args.plan is None:
                raise ValueError("A bounded proposal is required.")
            plan = RolloutPlan.model_validate(unique_json(limited_read(args.plan)))
            receipts = tuple(
                OfflineEvidence.model_validate(unique_json(limited_read(path))) for path in args.evidence
            )
            result = RolloutReviewer().review(plan, receipts)
            passed = result.status == "eligible_for_operator_review"
        else:
            if args.lane is None or (args.command == "collect" and args.report is None):
                raise ValueError("A lane and its test report are required.")
            result = (
                run(args.lane, artifacts)
                if args.command == "run"
                else collect(args.report, args.lane, artifacts)
            )
            passed = result.passed
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(result.model_dump_json(indent=2) + "\n")
        print(
            json.dumps(
                {"scope": "enterprise_offline_qualification", "passed": passed, "production_ready": False}
            )
        )
        return 0 if passed else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = (
            "runner_timeout"
            if isinstance(error, subprocess.TimeoutExpired)
            else "runner_or_source_failure"
            if isinstance(error, subprocess.SubprocessError)
            else "filesystem_unavailable"
            if isinstance(error, OSError)
            else "invalid_evidence_or_arguments"
        )
        print(
            json.dumps(
                {
                    "scope": "enterprise_offline_qualification",
                    "status": "failed",
                    "reason": reason,
                    "production_ready": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
