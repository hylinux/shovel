import json
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import pytest
from scripts import demo_enterprise as sample

from shovel.runtime.errors import RuntimeFailure


async def test_standalone_sample_uses_real_engine_and_safe_temporary_sqlite(tmp_path, monkeypatch, capsys):
    observed = {}

    @contextmanager
    def temporary(*, prefix):
        with TemporaryDirectory(prefix=prefix, dir=tmp_path) as root:
            yield root
            with closing(sqlite3.connect(Path(root) / "sample.db")) as database:
                tables = database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                for (name,) in tables:
                    rows = database.execute(f'SELECT * FROM "{name}"').fetchall()
                    serialized = str(rows)
                    for sentinel in (sample.CREDENTIAL, sample.SOURCE_TEXT, sample.DERIVED_TEXT):
                        assert sentinel not in serialized
                observed["audits"] = database.execute("SELECT COUNT(*) FROM protection_audit").fetchone()[0]
                observed["profiles"] = database.execute(
                    "SELECT enabled, status FROM enterprise_connection_profile"
                ).fetchall()
                identities = database.execute(
                    "SELECT id, revision, created_at, updated_at, configuration FROM identity_profile"
                ).fetchall()
                observed["identities"] = [
                    sample.IdentityProfile(
                        id=row[0],
                        revision=row[1],
                        created_at=row[2],
                        updated_at=row[3],
                        **json.loads(row[4]),
                    )
                    for row in identities
                ]

    async def no_http(*_args, **_kwargs):
        pytest.fail("The synthetic sample must not send HTTP requests.")

    monkeypatch.setattr(sample, "TemporaryDirectory", temporary)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setenv("SHOVEL_DATABASE__URL", "invalid-user-store-must-not-be-loaded")
    frames = []
    report = await sample.run_sample(frames.append)
    assert report["synthetic_only"] is True and report["production_ready"] is False
    assert report["allow"] == "succeeded"
    assert report["deny"] == {"state": "failed", "error": "data_use_denied"}
    assert report["authentication"] == {
        "waiting": "waiting_for_authentication",
        "automatic_resumes": 0,
        "resumed": "succeeded",
        "request": "satisfied",
        "same_deadline": True,
    }
    assert report["derived"] == {
        "state": "succeeded",
        "receipt": {"status": "derived", "embedding": "denied"},
        "source_bound": True,
        "source_count": 1,
        "classification": "sample.internal",
        "rights": ["display", "read", "transform"],
        "body_recovery": "unavailable",
    }
    assert frames == [sample.SOURCE_TEXT, sample.SOURCE_TEXT]
    assert report["display_frames"] == 2 and report["broker_calls"] == 5 and report["transport_calls"] == 4
    assert report["audit_decisions"] == observed["audits"] == 8
    assert observed["profiles"] == [(0, "draft")]
    assert observed["identities"] and all(not identity.enabled for identity in observed["identities"])
    assert all(identity.broker_type == sample.BrokerType.UNAVAILABLE for identity in observed["identities"])
    assert list(tmp_path.iterdir()) == []
    captured = capsys.readouterr()
    diagnostics = captured.out + captured.err + json.dumps(report)
    for sentinel in (sample.CREDENTIAL, sample.SOURCE_TEXT, sample.DERIVED_TEXT):
        assert sentinel not in diagnostics


async def test_display_failure_is_reported_without_claiming_sample_success(capsys):
    def broken_display(_value):
        raise RuntimeError(f"{sample.CREDENTIAL}: {sample.SOURCE_TEXT}")

    with pytest.raises(RuntimeFailure, match="ALLOW scenario failed"):
        await sample.run_sample(broken_display)
    captured = capsys.readouterr()
    assert sample.CREDENTIAL not in captured.out + captured.err
    assert sample.SOURCE_TEXT not in captured.out + captured.err


async def test_synthetic_publication_is_hash_bound_and_does_not_change_default_admission():
    contract = sample.FunctionContract(
        implementation_id=sample.READ,
        connection_requirements=(sample.ConnectionRequirement(name="source", connection_ref="synthetic"),),
    )
    content = sample.sample_content(contract)
    publisher = sample.SyntheticPublisher((contract,))
    await publisher.authorize(sample.SCOPE, "publish", "synthetic", 1, content.content_hash)
    changed = content.model_copy(update={"connection_requirements": ()})
    with pytest.raises(sample.SecurityDenied):
        await publisher.authorize(sample.SCOPE, "publish", "synthetic", 1, changed.content_hash)
    with pytest.raises(sample.SecurityDenied, match="Connection-backed execution is not enabled"):
        sample.PolicyEvaluator().evaluate(
            content, sample.RiskAssessment(level="low", side_effect="none"), sample.UserSecurityPolicy()
        )


def test_documented_sample_module_command_runs_without_pytest_or_user_configuration():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.demo_enterprise"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.count(sample.SOURCE_TEXT) == 2
    assert '"synthetic_only": true' in result.stdout and '"production_ready": false' in result.stdout
    assert sample.CREDENTIAL not in result.stdout + result.stderr
    assert sample.DERIVED_TEXT not in result.stdout + result.stderr
