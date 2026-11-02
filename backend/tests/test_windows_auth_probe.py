import json

import pytest
from scripts import probe_windows_auth as probe


class PublicClient:
    def __init__(self, client_id, *, enable_broker_on_windows=None):
        pytest.fail("The probe must not construct a client.")

    def get_accounts(self, username=None):
        pytest.fail("The probe must not access accounts.")

    def acquire_token_silent_with_error(self, scopes, account, force_refresh=False, claims_challenge=None):
        pytest.fail("The probe must not acquire tokens.")

    def acquire_token_interactive(
        self,
        scopes,
        claims_challenge=None,
        timeout=None,
        parent_window_handle=None,
        on_before_launching_ui=None,
    ):
        pytest.fail("The probe must not show authentication UI.")

    def remove_account(self, account):
        pytest.fail("The probe must not disconnect accounts.")


@pytest.fixture
def offline_probe(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "win32")
    monkeypatch.setattr(probe.sys, "version_info", (3, 14, 0))
    monkeypatch.setattr(probe.sysconfig, "get_platform", lambda: "win-amd64")
    monkeypatch.setattr(
        probe, "inspect_packages", lambda: [probe.check("fixture_packages", True, "Test only.")]
    )
    monkeypatch.setattr(probe, "load_public_client", lambda: PublicClient)


def test_probe_inspects_public_shapes_without_authentication_or_production_acceptance(offline_probe):
    result = probe.probe()
    assert result["offline_checks_passed"]
    assert not result["production_ready"]
    assert not result["authentication_attempted"]
    assert any(item["status"] == "blocked" for item in result["production_gates"])
    assert all(item["status"] == "passed" for item in result["checks"])


def test_unsupported_public_signature_is_not_accepted_via_kwargs():
    class UnsupportedClient(PublicClient):
        def acquire_token_interactive(self, scopes, **kwargs):
            pytest.fail("No SDK method should execute.")

    checks = probe.inspect_public_client(UnsupportedClient)
    assert (
        next(item for item in checks if item["name"] == "public_api.acquire_token_interactive")["status"]
        == "failed"
    )


def test_sdk_exception_is_reported_as_failure_without_exposing_its_message(offline_probe, monkeypatch):
    def fail():
        raise RuntimeError("AUTH-SENTINEL")

    monkeypatch.setattr(probe, "load_public_client", fail)
    report = probe.probe()
    assert not report["offline_checks_passed"]
    assert "AUTH-SENTINEL" not in json.dumps(report)
    assert any(
        item["name"] == "offline_inspection" and item["status"] == "failed" for item in report["checks"]
    )


def test_missing_pinned_dependency_prevents_sdk_import(offline_probe, monkeypatch):
    monkeypatch.setattr(probe, "inspect_packages", lambda: [probe.check("package.msal", False, "Missing.")])
    monkeypatch.setattr(
        probe,
        "load_public_client",
        lambda: pytest.fail("Import must not run with missing dependencies."),
    )
    assert not probe.probe()["offline_checks_passed"]


@pytest.mark.parametrize("platform,abi", [("linux", "linux-x86_64"), ("win32", "win-arm64")])
def test_unsupported_platform_or_process_abi_does_not_load_sdk(offline_probe, monkeypatch, platform, abi):
    monkeypatch.setattr(probe.sys, "platform", platform)
    monkeypatch.setattr(probe.sysconfig, "get_platform", lambda: abi)
    monkeypatch.setattr(
        probe,
        "load_public_client",
        lambda: pytest.fail("Unsupported hosts must not load the SDK."),
    )
    assert not probe.probe()["offline_checks_passed"]


def test_probe_rejects_authentication_arguments_without_echoing_them(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--token=AUTH-SENTINEL"])
    assert probe.main() == 2
    output = capsys.readouterr()
    assert "AUTH-SENTINEL" not in output.out + output.err
