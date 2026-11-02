import copy
import json
import pickle

import pytest
from pydantic import SecretStr, ValidationError

from shovel.identity import errors
from shovel.identity.domain import AuthContext, IdentityBinding, IdentityProfile, TokenRequest
from shovel.identity.errors import safe_validation_errors

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
DIGEST = "a" * 64


def identity_binding() -> IdentityBinding:
    return IdentityBinding(
        workspace_id="workspace",
        user_id="owner",
        identity_profile_id="work",
        identity_revision=1,
        principal_ref="principal",
        tenant_id=TENANT,
    )


def token_request(**changes: object) -> TokenRequest:
    values = {
        "request_id": "request",
        "execution_id": "execution",
        "connection_profile_id": "connection",
        "connection_revision": 1,
        "provider_id": "provider",
        "provider_version": "1.0",
        "manifest_digest": DIGEST,
        "identity": identity_binding(),
        "authority": f"https://login.microsoftonline.com/{TENANT}",
        "audience": "api://resource",
        "allowed_scopes": ("api://resource/read",),
        "correlation_id": "correlation",
    }
    return TokenRequest.model_validate({**values, **changes})


@pytest.mark.parametrize(
    "authority",
    [
        "http://login.microsoftonline.com/common",
        f"https://user:credential@login.microsoftonline.com/{TENANT}",
        f"https://login.microsoftonline.com/{TENANT}?access_token=sentinel",
        "https://login.microsoftonline.com/common",
        f"https://login.microsoftonline.com/{OTHER_TENANT}",
        f"https://login.microsoftonline.com:invalid/{TENANT}",
    ],
)
def test_token_request_rejects_unsafe_or_wrong_tenant_authority(authority: str) -> None:
    with pytest.raises(ValidationError):
        token_request(authority=authority)


def test_profile_and_request_are_validated_immutable_and_versioned() -> None:
    profile = IdentityProfile(
        id="work", display_name="Work", tenant_binding=TENANT, created_at=1, updated_at=2
    )
    assert IdentityProfile.model_validate_json(profile.model_dump_json()) == profile
    with pytest.raises(ValidationError):
        profile.enabled = True
    with pytest.raises(ValidationError):
        token_request(format_version=2)
    with pytest.raises(ValidationError):
        token_request(allowed_scopes=("read", "read"))
    with pytest.raises(ValidationError):
        token_request(force_refresh="true")


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_scopes": ("api://resource/write",)},
        {"audience": "api://other"},
        {"connection_revision": 2},
        {"identity": identity_binding().model_copy(update={"principal_ref": "other"})},
        {"interaction_mode": "interactive"},
        {"force_refresh": True},
        {"claims_challenge_ref": "untrusted"},
    ],
)
def test_request_cannot_expand_a_resolved_ceiling(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        token_request(**changes).validate_ceiling(token_request())


def test_interaction_and_refresh_require_separate_explicit_authorization() -> None:
    token_request(interaction_mode="interactive", force_refresh=True).validate_ceiling(
        token_request(),
        interactive_allowed=True,
        refresh_allowed=True,
    )


@pytest.mark.parametrize("serialize", [repr, str, json.dumps, pickle.dumps, copy.copy, copy.deepcopy, vars])
def test_auth_context_is_redacted_and_cannot_be_serialized(serialize) -> None:
    context = AuthContext(token_request(), SecretStr("AUTH-SENTINEL"), expires_at=20)
    if serialize in {repr, str}:
        assert "AUTH-SENTINEL" not in serialize(context)
    else:
        with pytest.raises(TypeError):
            serialize(context)


def test_auth_context_is_exactly_bound_and_one_use_even_when_transport_fails() -> None:
    request = token_request()
    context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)
    received = []
    assert (
        context.consume(request, now=10, transport=lambda secret: received.append(secret.get_secret_value()))
        is None
    )
    assert received == ["AUTH-SENTINEL"]
    with pytest.raises(ValueError):
        context.consume(request, now=10, transport=lambda _: pytest.fail("Consumed context"))
    context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)
    with pytest.raises(ValueError):
        context.consume(
            token_request(connection_revision=2), now=10, transport=lambda _: pytest.fail("Wrong binding")
        )
    with pytest.raises(ValueError):
        context.consume(request, now=10, transport=lambda _: pytest.fail("Failed attempt must close"))


def test_transport_failure_closes_context_without_replacing_error() -> None:
    request = token_request()
    context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)

    def failed(_):
        raise RuntimeError("transport failed")

    with pytest.raises(RuntimeError, match="transport failed"):
        context.consume(request, now=10, transport=failed)
    with pytest.raises(ValueError):
        context.consume(request, now=10, transport=lambda _: pytest.fail("Closed context"))


def test_transport_cannot_reenter_auth_context_or_use_nonfinite_time() -> None:
    request = token_request()
    context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)

    def reenter(_):
        with pytest.raises(ValueError):
            context.consume(request, now=10, transport=lambda _: pytest.fail("Reentrant use"))

    context.consume(request, now=10, transport=reenter)
    for now in (20, -1, float("nan"), float("inf")):
        context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)
        with pytest.raises(ValueError):
            context.consume(request, now=now, transport=lambda _: pytest.fail("Invalid time"))


def test_safe_errors_and_validation_do_not_echo_input() -> None:
    error_types = (
        errors.AuthenticationRequiredError,
        errors.ReauthenticationRequiredError,
        errors.ClaimsChallengeError,
        errors.WrongTenantError,
        errors.TokenAudienceMismatchError,
        errors.AccountNotFoundError,
        errors.BrokerUnavailableError,
        errors.ConditionalAccessBlockedError,
        errors.DeviceComplianceRequiredError,
        errors.ConsentRequiredError,
        errors.EntitlementRequiredError,
        errors.JitActivationRequiredError,
        errors.ExternalAuthorizationDeniedError,
        errors.AuthConfigurationError,
    )
    for kind in error_types:
        error = kind(correlation_id="correlation", diagnostic_ref="diagnostic")
        assert error.safe_dump()["code"] == error.code
        assert error.__cause__ is None
    with pytest.raises(ValidationError) as caught:
        token_request(access_token="AUTH-SENTINEL")
    assert "AUTH-SENTINEL" not in json.dumps(safe_validation_errors(caught.value))
