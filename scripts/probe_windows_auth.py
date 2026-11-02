# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#     "msal[broker]==1.39.0; sys_platform == 'win32'",
#     "pymsalruntime==0.20.7; sys_platform == 'win32'",
#     "pydantic==2.14.1",
# ]
#
# [[tool.uv.index]]
# url = "https://packagefeedproxy.microsoft.io/pypi/simple/"
# default = true
# ///
"""Offline ES-5 SDK inspection, not an authentication adapter or production acceptance."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import sysconfig
from importlib import metadata
from pathlib import Path
from typing import Literal, TypedDict

type Status = Literal["passed", "failed", "blocked", "not_verified"]


class Check(TypedDict):
    name: str
    status: Status
    detail: str


class Report(TypedDict):
    format_version: int
    scope: str
    offline_checks_passed: bool
    production_ready: bool
    authentication_attempted: bool
    checks: list[Check]
    production_gates: list[Check]


def check(name: str, passed: bool, detail: str) -> Check:
    return {"name": name, "status": "passed" if passed else "failed", "detail": detail}


def inspect_public_client(client: object) -> list[Check]:
    """Inspect named public parameters; never construct clients or invoke SDK methods."""
    required = {
        "__init__": {"client_id", "enable_broker_on_windows"},
        "get_accounts": {"username"},
        "acquire_token_silent_with_error": {"scopes", "account", "force_refresh", "claims_challenge"},
        "acquire_token_interactive": {
            "scopes",
            "claims_challenge",
            "timeout",
            "parent_window_handle",
            "on_before_launching_ui",
        },
        "remove_account": {"account"},
    }
    results = []
    for name, parameters in required.items():
        method = getattr(client, name, None)
        if not callable(method):
            results.append(check(f"public_api.{name}", False, "Required public method is missing."))
            continue
        try:
            signature = inspect.signature(method)
        except TypeError, ValueError:
            results.append(check(f"public_api.{name}", False, "Public signature cannot be inspected."))
            continue
        supported = {
            key
            for key, value in signature.parameters.items()
            if value.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        results.append(
            check(
                f"public_api.{name}",
                parameters.issubset(supported),
                "Named public parameters only; **kwargs is not evidence of support.",
            )
        )
    return results


def inspect_packages() -> list[Check]:
    results = []
    for package, expected in (("msal", "1.39.0"), ("pymsalruntime", "0.20.7"), ("pydantic", "2.14.1")):
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            results.append(
                check(f"package.{package}", False, f"Required pinned version {expected} is missing.")
            )
        else:
            results.append(check(f"package.{package}", actual == expected, f"Required version: {expected}."))
    try:
        wheel = metadata.distribution("pymsalruntime").read_text("WHEEL")
    except metadata.PackageNotFoundError:
        wheel = None
    results.append(
        check(
            "broker_wheel_tag",
            wheel is not None and "Tag: cp314-cp314-win_amd64" in wheel.splitlines(),
            "Installed CPython 3.14 x64 wheel metadata only; native loading is not tested.",
        )
    )
    return results


def load_public_client() -> object:
    sdk = importlib.import_module("msal")
    return getattr(sdk, "PublicClientApplication", None)


def inspect_binding_guards() -> list[Check]:
    from shovel.identity.domain import IdentityBinding, TokenRequest

    tenant = "11111111-1111-1111-1111-111111111111"
    values = {
        "request_id": "es5-fixture-request",
        "execution_id": "es5-fixture-execution",
        "connection_profile_id": "es5-fixture-connection",
        "connection_revision": 1,
        "provider_id": "es5-fixture-provider",
        "provider_version": "1.0",
        "manifest_digest": "a" * 64,
        "identity": IdentityBinding(
            workspace_id="es5-fixture",
            user_id="es5-fixture",
            identity_profile_id="es5-fixture",
            identity_revision=1,
            principal_ref="es5-fixture",
            tenant_id=tenant,
        ),
        "authority": f"https://login.microsoftonline.com/{tenant}",
        "audience": "api://es5-fixture",
        "allowed_scopes": ("api://es5-fixture/read",),
        "correlation_id": "es5-fixture-correlation",
    }
    approved = TokenRequest.model_validate(values)
    approved.validate_ceiling(approved)
    changes: dict[str, dict[str, object]] = {
        "tenant": {"authority": "https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222"},
        "audience": {"audience": "api://es5-other"},
        "scopes": {"allowed_scopes": ("api://es5-fixture/write",)},
        "connection_revision": {"connection_revision": 2},
        "manifest": {"manifest_digest": "b" * 64},
        "principal": {"identity": approved.identity.model_copy(update={"principal_ref": "es5-other"})},
        "correlation": {"correlation_id": "es5-other"},
        "interactive": {"interaction_mode": "interactive"},
        "refresh": {"force_refresh": True},
        "claims": {"claims_challenge_ref": "es5-untrusted"},
    }
    results = []
    for name, replacement in changes.items():
        try:
            TokenRequest.model_validate({**values, **replacement}).validate_ceiling(approved)
        except ValueError:
            rejected = True
        else:
            rejected = False
        results.append(
            check(
                f"synthetic_core_binding.{name}", rejected, "Existing core preflight only; no SDK invocation."
            )
        )
    return results


def production_gates() -> list[Check]:
    results: list[Check] = [
        {
            "name": "wam_required_before_acquisition",
            "status": "blocked",
            "detail": (
                "MSAL documents non-broker fallback on initialization failure; opt-in is not enforcement."
            ),
        },
    ]
    for name, detail in (
        (
            "native_loading",
            "Wheel compatibility does not prove WAM initialization or native loading.",
        ),
        (
            "account_principal_tenant",
            "Public stable account/principal binding, including guests, needs proof.",
        ),
        (
            "registration_and_consent",
            "No approved Entra registration, tenant or consent was exercised.",
        ),
        (
            "human_action_and_hwnd",
            "No interactive call, human confirmation or trusted HWND was exercised.",
        ),
        (
            "native_cancellation",
            "No SDK/UI cancellation was exercised; coroutine cancellation is insufficient.",
        ),
        (
            "cloud_ca_claims",
            "Global/China, guest, Conditional Access and trusted challenges are untested.",
        ),
        (
            "application_disconnect",
            "No application disconnect or Windows/Office account operation was attempted.",
        ),
        (
            "secret_safe_sdk_diagnostics",
            "Real SDK/native errors and telemetry require separate acceptance.",
        ),
    ):
        results.append({"name": name, "status": "not_verified", "detail": detail})
    return results


def probe() -> Report:
    checks = [
        check("windows", sys.platform == "win32", "Windows-only spike; no other OS adapter."),
        check("python_abi", sys.version_info[:2] == (3, 14), "This spike locks the CPython 3.14 ABI."),
        check(
            "process_architecture",
            sysconfig.get_platform() == "win-amd64",
            "Requires x64 Python, including emulation.",
        ),
    ]
    if all(item["status"] == "passed" for item in checks):
        checks.extend(inspect_packages())
        if all(item["status"] == "passed" for item in checks):
            try:
                checks.extend(inspect_public_client(load_public_client()))
                checks.extend(inspect_binding_guards())
            except Exception as error:
                checks.append(
                    check(
                        "offline_inspection",
                        False,
                        f"Inspection failed with {type(error).__name__}; details omitted.",
                    )
                )
    return {
        "format_version": 1,
        "scope": "offline_sdk_inspection_only",
        "offline_checks_passed": all(item["status"] == "passed" for item in checks),
        "production_ready": False,
        "authentication_attempted": False,
        "checks": checks,
        "production_gates": production_gates(),
    }


def main() -> int:
    if len(sys.argv) != 1:
        print(
            "This offline probe accepts no account, tenant, credential or authentication arguments.",
            file=sys.stderr,
        )
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))
    report = probe()
    print(json.dumps(report, indent=2))
    return 0 if report["offline_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
